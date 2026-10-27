import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, config, db, org_auth
from app.routes.org_requests import router

BASE = "/api/v1"


@pytest.fixture
def org_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "org-requests.db")
    db.init_db()
    now = auth._utcnow()
    headers = {}
    with db.connection() as conn:
        for student_id, role in (("alice", "member"), ("bob", "member"), ("admin", "admin"), ("applicant", "applicant")):
            conn.execute(
                "INSERT INTO account (student_id, role, pw_hash, pw_changed, created_at, updated_at)"
                " VALUES (?, ?, 'test-password-hash', 1, ?, ?)",
                (student_id, role, now.isoformat(), now.isoformat()),
            )
            token = auth.issue_session(conn, role, None, student_id)
            headers[student_id] = {"Authorization": f"Bearer {token}"}
            principal_id = org_auth.principal_for(conn, student_id)
            if student_id == "alice":
                for name, scopes in (("cli", ["applications:create", "applications:read:self"]), ("readonly", ["applications:read:self"])):
                    conn.execute(
                        "INSERT INTO org_cli_grant (id, principal_id, token_hash, scopes_json, credential_hash, created_at, expires_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (name, principal_id, org_auth.digest(name + "-token"), json.dumps(scopes),
                         org_auth.digest("test-password-hash"), now.isoformat(), (now + timedelta(hours=1)).isoformat()),
                    )
                    headers[name] = {"Authorization": f"Bearer {name}-token"}
        for project_id, visibility, recruiting in (("open", "member", 1), ("closed", "member", 0), ("private", "restricted", 1)):
            conn.execute(
                "INSERT INTO org_project (id, slug, title, description, lifecycle_status, visibility, recruiting, updated_at)"
                " VALUES (?, ?, ?, '', 'recruiting', ?, ?, ?)",
                (project_id, project_id, project_id, visibility, recruiting, now.isoformat()),
            )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, headers


def body(**fields):
    return {"kind": "project-join", "form_revision": "1", "fields": {
        "project_id": "open", "motivation": "  I would like to help this project.  ", "weekly_hours": 4, **fields,
    }}


def draft(client, headers, who="alice", **fields):
    response = client.post(BASE + "/applications/drafts", json=body(**fields), headers=headers[who])
    assert response.status_code == 201, response.text
    assert "confirmation" not in response.json()
    return response.json()["id"]


def confirm(client, headers, request_id):
    response = client.post(f"{BASE}/applications/{request_id}/confirmations", headers=headers["alice"])
    assert response.status_code == 201, response.text
    saved = client.get(f"{BASE}/applications/{request_id}", headers=headers["alice"]).json()
    assert json.dumps(response.json()["preview"]) == json.dumps(saved["preview"])
    return response.json()["confirmation"]


def submit(client, headers, request_id, token, key="test-key", who="alice"):
    return client.post(BASE + "/applications", json={"draft_id": request_id, "confirmation": token},
                       headers={**headers[who], "Idempotency-Key": key})


def submitted(client, headers):
    request_id = draft(client, headers)
    response = submit(client, headers, request_id, confirm(client, headers, request_id))
    assert response.status_code == 201, response.text
    return request_id


def test_schema_validation_normalization_and_no_side_effects(org_client):
    client, headers = org_client
    response = client.get(BASE + "/forms/project-join/schema", headers=headers["alice"])
    assert response.status_code == 200
    assert {"revision", "source_ref", "updated_at", "fields"} <= response.json().keys()
    with db.connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM service_request").fetchone()[0]
    response = client.post(BASE + "/applications/validate", json=body(notes=" x\r\ny "), headers=headers["cli"])
    assert response.status_code == 200, response.text
    assert response.json()["preview"]["fields"]["notes"] == "x\ny"
    assert response.json()["preview"]["fields"]["motivation"] == "I would like to help this project."
    assert "confirmation" not in response.json()
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM service_request").fetchone()[0] == before == 0
        assert conn.execute("SELECT COUNT(*) FROM org_confirmation").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM org_idempotency").fetchone()[0] == 0


@pytest.mark.parametrize("fields,status", [
    ({"motivation": "short"}, 422), ({"weekly_hours": True}, 422),
    ({"weekly_hours": "4"}, 422), ({"weekly_hours": 0}, 422), ({"weekly_hours": 41}, 422),
    ({"phone": "sensitive"}, 422), ({"student_id": "sensitive"}, 422),
    ({"notes": "x" * 2001}, 422), ({"project_id": "missing"}, 404),
    ({"project_id": "private"}, 404), ({"project_id": "closed"}, 409),
])
def test_invalid_or_inaccessible_project(org_client, fields, status):
    client, headers = org_client
    for endpoint in ("/applications/validate", "/applications/drafts"):
        response = client.post(BASE + endpoint, json=body(**fields), headers=headers["alice"])
        assert response.status_code == status, response.text
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM service_request").fetchone()[0] == 0


def test_old_revision_and_extra_envelope_rejected(org_client):
    client, headers = org_client
    value = body()
    value["form_revision"] = "old"
    assert client.post(BASE + "/applications/validate", json=value, headers=headers["alice"]).status_code == 409
    value = body()
    value["confirmation"] = "agent-created-token"
    assert client.post(BASE + "/applications/drafts", json=value, headers=headers["alice"]).status_code == 422


def test_cli_can_draft_but_cannot_confirm_or_review(org_client):
    client, headers = org_client
    request_id = draft(client, headers, who="cli")
    assert client.get(f"{BASE}/applications/{request_id}", headers=headers["alice"]).status_code == 200
    assert client.post(f"{BASE}/applications/{request_id}/confirmations", headers=headers["cli"]).status_code == 403
    assert client.post(f"{BASE}/applications/{request_id}/review", json={"decision": "approved", "reason": "test"}, headers=headers["cli"]).status_code == 403
    assert submit(client, headers, request_id, "forged", who="cli").status_code == 409
    assert submit(client, headers, request_id, confirm(client, headers, request_id), who="cli").status_code == 201


def test_ownership_is_enforced_for_all_draft_paths(org_client):
    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    assert client.get(f"{BASE}/applications/{request_id}", headers=headers["bob"]).status_code == 404
    assert client.get(BASE + "/applications", headers=headers["bob"]).json() == {"items": [], "next_cursor": None}
    assert client.post(f"{BASE}/applications/{request_id}/confirmations", headers=headers["bob"]).status_code == 404
    assert client.post(BASE + "/applications/drafts", json={**body(), "draft_id": request_id}, headers=headers["bob"]).status_code == 404
    assert submit(client, headers, request_id, token, who="bob").status_code == 404
    assert client.post(f"{BASE}/applications/{request_id}/withdraw", headers=headers["bob"]).status_code == 404
    assert submit(client, headers, request_id, token).status_code == 201


def test_pagination_is_bounded_and_owner_only(org_client):
    client, headers = org_client
    expected = {draft(client, headers) for _ in range(3)}
    draft(client, headers, who="bob")
    seen, cursor = set(), None
    while True:
        response = client.get(BASE + "/applications", params={"limit": 1, **({"cursor": cursor} if cursor else {})}, headers=headers["alice"])
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1
        seen.add(response.json()["items"][0]["id"])
        cursor = response.json()["next_cursor"]
        if not cursor:
            break
    assert seen == expected
    assert client.get(BASE + "/applications?limit=101", headers=headers["alice"]).status_code == 422


@pytest.mark.parametrize("mutation", ["expired", "payload", "revision", "project", "updated", "rotated", "other_draft"])
def test_confirmation_is_short_lived_and_content_bound(org_client, mutation):
    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    with db.connection() as conn:
        stored = conn.execute("SELECT token_hash FROM org_confirmation").fetchone()[0]
        assert stored != token
        assert stored == org_auth.digest(token)
        if mutation == "expired":
            conn.execute("UPDATE org_confirmation SET expires_at = ?", ((auth._utcnow() - timedelta(seconds=1)).isoformat(),))
        elif mutation == "payload":
            conn.execute("UPDATE service_request SET payload_json = ? WHERE id = ?", (json.dumps(body(motivation="Changed content after confirmation")["fields"]), request_id))
        elif mutation == "revision":
            conn.execute("UPDATE service_request SET form_revision = '2' WHERE id = ?", (request_id,))
        elif mutation == "project":
            conn.execute("UPDATE service_request SET project_id = 'different' WHERE id = ?", (request_id,))
    if mutation == "updated":
        response = client.post(BASE + "/applications/drafts", json={**body(notes="changed"), "draft_id": request_id}, headers=headers["cli"])
        assert response.status_code == 201
    elif mutation == "rotated":
        assert confirm(client, headers, request_id) != token
    elif mutation == "other_draft":
        request_id = draft(client, headers)
    assert submit(client, headers, request_id, token).status_code == 409
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM service_request WHERE status='submitted'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM org_idempotency").fetchone()[0] == 0


def test_closed_project_rechecked_at_confirmation_and_submission(org_client):
    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    with db.connection() as conn:
        conn.execute("UPDATE org_project SET recruiting = 0 WHERE id = 'open'")
    assert client.post(f"{BASE}/applications/{request_id}/confirmations", headers=headers["alice"]).status_code == 409
    assert submit(client, headers, request_id, token).status_code == 409


def test_idempotency_conflict_replay_and_single_consumption(org_client):
    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    response = submit(client, headers, request_id, token)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "submitted"
    assert submit(client, headers, request_id, token, key="new-key").status_code == 409
    assert submit(client, headers, request_id, "different-token").status_code == 409
    with db.connection() as conn:
        conn.execute("UPDATE org_confirmation SET expires_at = ?", ((auth._utcnow() - timedelta(hours=1)).isoformat(),))
        assert conn.execute("SELECT used_at FROM org_confirmation").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM org_idempotency").fetchone()[0] == 1
    assert client.post(f"{BASE}/applications/{request_id}/review", json={"decision": "approved", "reason": "Reviewed"}, headers=headers["admin"]).status_code == 200
    replay = submit(client, headers, request_id, token)
    assert replay.status_code == response.status_code
    assert replay.json() == response.json()
    other = draft(client, headers, who="bob")
    other_token = client.post(f"{BASE}/applications/{other}/confirmations", headers=headers["bob"]).json()["confirmation"]
    assert submit(client, headers, other, other_token, who="bob").status_code == 201


@pytest.mark.parametrize("key", [None, "", " " * 5, "x" * 201])
def test_idempotency_key_required_and_bounded(org_client, key):
    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    response = client.post(BASE + "/applications", json={"draft_id": request_id, "confirmation": token},
                           headers={**headers["alice"], **({"Idempotency-Key": key} if key is not None else {})})
    assert response.status_code == 422
    assert submit(client, headers, request_id, token).status_code == 201


def test_submit_rejects_content_override(org_client):
    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    response = client.post(BASE + "/applications", json={"draft_id": request_id, "confirmation": token, "fields": body()["fields"]},
                           headers={**headers["alice"], "Idempotency-Key": "test"})
    assert response.status_code == 422


def test_review_state_machine_and_history(org_client):
    client, headers = org_client
    request_id = submitted(client, headers)
    def decide(decision, who="admin", reason="Reviewed with an explicit reason"):
        return client.post(f"{BASE}/applications/{request_id}/review", json={"decision": decision, "reason": reason}, headers=headers[who])
    assert decide("approved", who="alice").status_code == 403
    assert decide("in_review").status_code == 200
    assert decide("in_review").status_code == 409
    assert decide("needs_information").status_code == 200
    assert decide("approved").status_code == 409
    assert client.post(BASE + "/applications/drafts", json={**body(notes="additional information"), "draft_id": request_id}, headers=headers["alice"]).status_code == 201
    assert submit(client, headers, request_id, confirm(client, headers, request_id), key="resubmission").status_code == 201
    assert decide("approved", reason=" ").status_code == 422
    assert decide("approved").status_code == 200
    assert decide("approved").status_code == 409
    assert decide("rejected").status_code == 409
    assert client.post(BASE + "/applications/drafts", json={**body(), "draft_id": request_id}, headers=headers["alice"]).status_code == 409
    result = client.get(f"{BASE}/applications/{request_id}", headers=headers["alice"]).json()
    assert result["status"] == "approved"
    assert [review["decision"] for review in result["reviews"]] == ["in_review", "needs_information", "approved"]
    with db.connection() as conn:
        reviewer = conn.execute("SELECT id FROM org_principal WHERE student_id = 'admin'").fetchone()[0]
        assert all(review["reviewer_id"] == reviewer and review["decided_at"] for review in result["reviews"])
        row = conn.execute("SELECT reviewer_id, review_reason FROM service_request WHERE id = ?", (request_id,)).fetchone()
        assert row["reviewer_id"] == reviewer
        assert row["review_reason"] == "Reviewed with an explicit reason"


@pytest.mark.parametrize("outcome", ["withdrawn", "rejected"])
def test_terminal_states_cannot_be_reopened(org_client, outcome):
    client, headers = org_client
    request_id = submitted(client, headers)
    if outcome == "withdrawn":
        response = client.post(f"{BASE}/applications/{request_id}/withdraw", headers=headers["alice"])
    else:
        response = client.post(f"{BASE}/applications/{request_id}/review", json={"decision": "rejected", "reason": "Cannot accept"}, headers=headers["admin"])
    assert response.status_code == 200
    assert client.post(f"{BASE}/applications/{request_id}/confirmations", headers=headers["alice"]).status_code == 409
    assert client.post(f"{BASE}/applications/{request_id}/withdraw", headers=headers["alice"]).status_code == 409
    assert client.post(f"{BASE}/applications/{request_id}/review", json={"decision": "approved", "reason": "Cannot reopen"}, headers=headers["admin"]).status_code == 409


@pytest.mark.parametrize("revocation", ["role", "grant", "password", "expired", "session"])
def test_current_identity_checked_before_confirmation_submit_and_replay(org_client, revocation):
    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    assert submit(client, headers, request_id, token, who="cli").status_code == 201
    with db.connection() as conn:
        if revocation == "role":
            conn.execute("UPDATE account SET role = 'applicant' WHERE student_id = 'alice'")
        elif revocation == "grant":
            conn.execute("UPDATE org_cli_grant SET revoked_at = ? WHERE id = 'cli'", (auth._utcnow().isoformat(),))
        elif revocation == "password":
            conn.execute("UPDATE account SET pw_hash = 'changed-hash' WHERE student_id = 'alice'")
        elif revocation == "expired":
            conn.execute("UPDATE org_cli_grant SET expires_at = ? WHERE id = 'cli'", ((auth._utcnow() - timedelta(seconds=1)).isoformat(),))
        else:
            token_hash = org_auth.digest(headers["alice"]["Authorization"].split()[1])
            conn.execute("DELETE FROM login_session WHERE token_hash = ?", (token_hash,))
    who = "alice" if revocation == "session" else "cli"
    assert submit(client, headers, request_id, token, who=who).status_code in (401, 403)
    assert client.get(f"{BASE}/applications/{request_id}", headers=headers[who]).status_code in (401, 403)
    assert client.post(BASE + "/applications/drafts", json=body(), headers=headers[who]).status_code in (401, 403)


def test_no_member_permissions_for_applicant_anonymous_or_readonly_cli(org_client):
    client, headers = org_client
    for who in (None, "applicant", "readonly"):
        response = client.post(BASE + "/applications/drafts", json=body(), headers=headers[who] if who else {})
        assert response.status_code in (401, 403)
    assert client.get(BASE + "/applications", headers=headers["applicant"]).status_code == 403
    assert client.get(BASE + "/forms/project-join/schema").status_code == 401


def test_admin_demotion_blocks_review_and_draft_review_is_illegal(org_client):
    client, headers = org_client
    request_id = draft(client, headers)
    endpoint = f"{BASE}/applications/{request_id}/review"
    decision = {"decision": "approved", "reason": "No implicit submission"}
    assert client.post(endpoint, json=decision, headers=headers["admin"]).status_code == 409
    with db.connection() as conn:
        conn.execute("UPDATE account SET role = 'member' WHERE student_id = 'admin'")
    assert client.post(endpoint, json=decision, headers=headers["admin"]).status_code in (401, 403)


def test_concurrent_replay_submits_exactly_once(org_client):
    from concurrent.futures import ThreadPoolExecutor

    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: submit(client, headers, request_id, token), range(8)))
    assert all(response.status_code == 201 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM org_idempotency").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM org_confirmation WHERE used_at IS NOT NULL").fetchone()[0] == 1


def test_submission_failure_rolls_back_confirmation_and_state(org_client):
    import sqlite3

    client, headers = org_client
    request_id = draft(client, headers)
    token = confirm(client, headers, request_id)
    with db.connection() as conn:
        conn.execute("CREATE TRIGGER fail_idempotency BEFORE INSERT ON org_idempotency BEGIN SELECT RAISE(ABORT, 'test write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="test write failure"):
        submit(client, headers, request_id, token)
    with db.connection() as conn:
        assert conn.execute("SELECT status FROM service_request WHERE id = ?", (request_id,)).fetchone()[0] == "draft"
        assert conn.execute("SELECT used_at FROM org_confirmation").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM org_idempotency").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_idempotency")
    assert submit(client, headers, request_id, token).status_code == 201
