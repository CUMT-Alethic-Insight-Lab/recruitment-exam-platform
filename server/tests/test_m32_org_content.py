import base64
import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, config, db, org_auth
from app.routes.org_content import router
from scripts.sync_org_knowledge import DEFAULT_ALLOWLIST, main, sync_documents


@pytest.fixture
def content_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "content.db")
    db.init_db()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def identities(content_client):
    headers = {}
    now = auth._utcnow()
    with db.connection() as conn:
        for role in ("member", "admin", "applicant"):
            sid = "synthetic-" + role
            conn.execute(
                "INSERT INTO account (student_id, role, nickname, pw_hash, pw_changed, created_at, updated_at)"
                " VALUES (?, ?, '', 'synthetic-hash', 1, ?, ?)",
                (sid, role, now.isoformat(), now.isoformat()),
            )
            headers[role] = {"Authorization": "Bearer " + auth.issue_session(conn, role, None, sid)}
            if role != "applicant":
                principal_id = org_auth.principal_for(conn, sid)
                for label, scopes in (("cli-" + role, ["projects:read", "knowledge:read"]), ("no-scope-" + role, [])):
                    token = "synthetic-token-" + label
                    conn.execute(
                        "INSERT INTO org_cli_grant (id, principal_id, token_hash, scopes_json, credential_hash,"
                        " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (label, principal_id, org_auth.digest(token), json.dumps(scopes),
                         org_auth.digest("synthetic-hash"), now.isoformat(), (now + timedelta(hours=1)).isoformat()),
                    )
                    headers[label] = {"Authorization": "Bearer " + token}
    return headers


def seed_project(project_id, visibility="member", owner=None):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO org_project (id, slug, title, description, lifecycle_status, visibility,"
            " owner_student_id, recruiting, updated_at) VALUES (?, ?, ?, 'Synthetic description',"
            " 'proposed', ?, ?, 0, ?)",
            (project_id, project_id, "Synthetic " + project_id, visibility, owner, auth._utcnow().isoformat()),
        )


def seed_document(document_id, visibility="member", content="Synthetic policy text"):
    now = auth._utcnow().isoformat()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO org_document (id, source_repo, path, revision, visibility, source_url, title,"
            " content, indexed_at, updated_at) VALUES (?, 'synthetic', ?, 'test-revision', ?,"
            " 'https://example.org/policy', ?, ?, ?, ?)",
            (document_id, document_id + ".md", visibility, "Synthetic " + document_id, content, now, now),
        )


def test_projects_default_empty_and_admin_create_update(content_client, identities):
    c, h = content_client, identities
    assert c.get("/api/v1/projects", headers=h["member"]).json() == {"items": [], "next_cursor": None}
    response = c.post("/api/v1/projects", headers=h["admin"], json={"slug": "synthetic", "title": "Synthetic"})
    assert response.status_code == 201, response.text
    project = response.json()
    assert project["recruiting"] is False
    assert project["lifecycle"] == project["lifecycle_status"] == "proposed"
    assert project["visibility"] == "member"
    assert "owner_student_id" not in project
    response = c.patch("/api/v1/projects/" + project["id"], headers=h["admin"], json={"recruiting": True, "lifecycle": "recruiting"})
    assert response.status_code == 200, response.text
    assert response.json()["recruiting"] is True
    assert len(c.get("/api/v1/projects?recruiting=true", headers=h["member"]).json()["items"]) == 1
    assert c.post("/api/v1/projects", headers=h["admin"], json={"slug": "synthetic", "title": "Duplicate"}).status_code == 409
    assert c.patch("/api/v1/projects/" + project["id"], headers=h["admin"], json={"title": None}).status_code == 422
    assert c.post("/api/v1/projects", headers=h["admin"], json={"slug": "x", "title": " ", "visibility": "public"}).status_code == 422


@pytest.mark.parametrize("actor", ["member", "cli-member", "cli-admin", "applicant"])
def test_project_mutations_web_admin_only(content_client, identities, actor):
    seed_project("test")
    h = identities[actor]
    assert content_client.post("/api/v1/projects", headers=h, json={"slug": "new", "title": "New"}).status_code == 403
    assert content_client.patch("/api/v1/projects/test", headers=h, json={"title": "Changed"}).status_code == 403


def test_project_acl_pagination_and_revocation(content_client, identities):
    c, h = content_client, identities
    seed_project("a", "public")
    seed_project("b")
    seed_project("c", "project", "synthetic-member")
    seed_project("hidden", "project", "someone-else")
    seed_project("restricted", "restricted", "synthetic-member")
    first = c.get("/api/v1/projects?limit=2", headers=h["cli-member"]).json()
    assert [p["id"] for p in first["items"]] == ["a", "b"]
    second = c.get("/api/v1/projects", params={"limit": 2, "cursor": first["next_cursor"]}, headers=h["cli-member"]).json()
    assert [p["id"] for p in second["items"]] == ["c"]
    assert second["next_cursor"] is None
    for project_id in ("hidden", "restricted", "missing"):
        assert c.get("/api/v1/projects/" + project_id, headers=h["cli-member"]).status_code == 404
    assert c.get("/api/v1/projects/restricted", headers=h["cli-admin"]).status_code == 404
    assert c.get("/api/v1/projects/restricted", headers=h["admin"]).status_code == 200
    with db.connection() as conn:
        conn.execute("UPDATE org_project SET owner_student_id = 'someone-else' WHERE id = 'c'")
    assert c.get("/api/v1/projects/c", headers=h["cli-member"]).status_code == 404
    assert c.get("/api/v1/projects", headers=h["applicant"]).status_code == 403
    assert c.get("/api/v1/projects", headers=h["no-scope-member"]).status_code == 403
    assert c.get("/api/v1/projects").status_code == 401


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"cursor": "garbage"},
    {"cursor": base64.urlsafe_b64encode(b'{}').decode()},
    {"cursor": base64.urlsafe_b64encode(b'["projects", 1]').decode()},
])
def test_project_pagination_rejects_invalid(content_client, identities, params):
    assert content_client.get("/api/v1/projects", params=params, headers=identities["member"]).status_code == 422


def test_knowledge_same_acl_search_read_revocation_and_metadata(content_client, identities):
    c, h = content_client, identities
    seed_document("a", "public", "policy " + "a" * 1000)
    seed_document("b")
    seed_document("restricted", "restricted", "policy confidential")
    seed_document("project", "project", "policy internal")
    response = c.get("/api/v1/knowledge/search", params={"q": "policy", "limit": 1}, headers=h["cli-admin"])
    assert response.status_code == 200, response.text
    first = response.json()
    item = first["items"][0]
    assert set(item) == {"id", "title", "source_url", "revision", "updated_at", "access_level", "snippet"}
    assert len(item["snippet"]) <= 240
    second = c.get("/api/v1/knowledge/search", params={"q": "policy", "limit": 1, "cursor": first["next_cursor"]}, headers=h["cli-admin"]).json()
    assert [d["id"] for d in second["items"]] == ["b"]
    assert second["next_cursor"] is None
    assert c.get("/api/v1/knowledge/documents/a", headers=h["member"]).json()["access_level"] == "public"
    for document_id in ("restricted", "project", "missing"):
        assert c.get("/api/v1/knowledge/documents/" + document_id, headers=h["cli-admin"]).status_code == 404
    assert c.get("/api/v1/knowledge/documents/restricted", headers=h["admin"]).status_code == 200
    with db.connection() as conn:
        conn.execute("UPDATE org_document SET visibility = 'restricted' WHERE id = 'a'")
    assert c.get("/api/v1/knowledge/documents/a", headers=h["member"]).status_code == 404
    result = c.get("/api/v1/knowledge/search?q=policy", headers=h["member"]).json()
    assert [item["id"] for item in result["items"]] == ["b"]
    assert c.get("/api/v1/knowledge/search?q=policy", headers=h["no-scope-member"]).status_code == 403
    assert c.get("/api/v1/knowledge/documents/b", headers=h["applicant"]).status_code == 403
    assert c.get("/api/v1/knowledge/search", params={"q": "other", "cursor": first["next_cursor"]}, headers=h["member"]).status_code == 422


@pytest.mark.parametrize("q", ["", " ", "x" * 201])
def test_query_bounds(content_client, identities, q):
    assert content_client.get("/api/v1/knowledge/search", params={"q": q}, headers=identities["member"]).status_code == 422


def test_query_like_wildcards_are_literal(content_client, identities):
    seed_document("literal", content="100% policy_use \\ safe")
    seed_document("other", content="policy no wildcard")
    for query in ("%", "_", "\\", "' OR 1=1 --"):
        result = content_client.get("/api/v1/knowledge/search", params={"q": query}, headers=identities["member"])
        assert result.status_code == 200
        assert [d["id"] for d in result.json()["items"]] == ([] if query.startswith("'") else ["literal"])


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "public-source"
    for item in DEFAULT_ALLOWLIST["documents"]:
        path = root / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic policy; no real organizational rules.", encoding="utf-8")
    (root / "unknown.md").write_text("Unapproved must not be indexed", encoding="utf-8")
    return root


def test_sync_idempotent_sources_and_removed_allowlist(content_client, source, identities):
    assert sync_documents(source)["imported"] == 3
    with db.connection() as conn:
        before = [dict(row) for row in conn.execute("SELECT * FROM org_document ORDER BY id")]
    assert all(row["source_repo"] == "governance-public" for row in before)
    assert all(row["revision"].startswith("sha256:") for row in before)
    assert all(row["source_url"].startswith("https://github.com/CUMT-Alethic-Insight-Lab/governance-public/blob/main/") for row in before)
    sync_documents(source)
    with db.connection() as conn:
        assert before == [dict(row) for row in conn.execute("SELECT * FROM org_document ORDER BY id")]
    manifest = {"source_repo": "governance-public", "documents": [{**DEFAULT_ALLOWLIST["documents"][0], "access_level": "member"}]}
    commit = "a" * 40
    sync_documents(source, manifest, commit)
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM org_document").fetchall()
    assert len(rows) == 1
    assert rows[0]["revision"] == commit
    assert "/blob/" + commit + "/" in rows[0]["source_url"]
    assert rows[0]["visibility"] == "member"
    for old in before:
        expected = 200 if old["id"] == rows[0]["id"] else 404
        assert content_client.get("/api/v1/knowledge/documents/" + old["id"], headers=identities["member"]).status_code == expected
    sync_documents(source, {"source_repo": "governance-public", "documents": []})
    assert content_client.get("/api/v1/knowledge/search?q=Synthetic", headers=identities["member"]).json()["items"] == []


@pytest.mark.parametrize("mutation", ["unknown", "restricted", "project", "traversal", "hidden", "missing"])
def test_sync_refuses_unapproved_and_unsafe_sources(content_client, source, mutation):
    sync_documents(source)
    manifest = json.loads(json.dumps(DEFAULT_ALLOWLIST))
    if mutation == "unknown":
        manifest["source_repo"] = "private-or-unknown"
    elif mutation in ("restricted", "project"):
        manifest["documents"][0]["access_level"] = mutation
    else:
        manifest["documents"][0]["path"] = {"traversal": "../escape.md", "hidden": ".secret.md", "missing": "missing.md"}[mutation]
    with pytest.raises((ValueError, OSError)):
        sync_documents(source, manifest)
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM org_document").fetchone()[0] == 3


def test_sync_cli_local_source(content_client, source, capsys):
    main(["--local-dir", str(source), "--revision", "b" * 40])
    assert json.loads(capsys.readouterr().out) == {"source_repo": "governance-public", "imported": 3}
