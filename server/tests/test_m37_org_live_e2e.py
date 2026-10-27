import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from fastapi import Request as FastAPIRequest

from app import config
from app.org_auth import require_org_identity

ROOT = Path(__file__).resolve().parents[2]


def call(base, method, path, token=None, body=None, idempotency=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers)


@pytest.fixture
def live_org(tmp_path):
    database = tmp_path / "live.db"
    port_socket = __import__("socket").socket()
    port_socket.bind(("127.0.0.1", 0))
    port = port_socket.getsockname()[1]
    port_socket.close()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "ALE_THIC_DB_PATH": str(database),
        "ALE_THIC_DATA_DIR": str(tmp_path),
        "ALE_THIC_ROLE": "authoritative",
        "ALE_THIC_CAPTCHA_ENABLED": "0",
        "SECRET_KEY": "local-live-e2e-only",
        "PUBLIC_PREFIX": "",
    })
    subprocess.run(
        [sys.executable, "-c", "from app import db; db.init_db()"],
        cwd=ROOT / "server", env=env, check=True, capture_output=True,
    )
    now = "2026-10-27T00:00:00+00:00"
    tokens = {"member": secrets.token_urlsafe(24), "admin": secrets.token_urlsafe(24)}
    with sqlite3.connect(database) as conn:
        for role in ("member", "admin"):
            student_id = role + "-live"
            conn.execute(
                "INSERT INTO account (student_id,role,nickname,pw_hash,pw_changed,created_at,updated_at)"
                " VALUES (?,?,?,'live-password',1,?,?)",
                (student_id, role, role + " display", now, now),
            )
            conn.execute(
                "INSERT INTO login_session (token_hash,kind,campaign_id,student_id,created_at,expires_at)"
                " VALUES (?,?,NULL,?,?,'2099-01-01T00:00:00+00:00')",
                (hashlib.sha256(tokens[role].encode()).hexdigest(), role, student_id, now),
            )
        conn.execute(
            "INSERT INTO org_document (id,source_repo,path,revision,visibility,source_url,title,content,indexed_at,updated_at)"
            " VALUES ('doc-live','governance-public','docs/zh-CN/membership.md','deadbeef','public',"
            "'https://example.invalid/membership','成员制度','成员应当尊重协作者并保护基础设施。',?,?)",
            (now, now),
        )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT / "server", env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError("uvicorn exited before the live test")
            try:
                with urlopen(base + "/api/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)
        yield base, tokens, tmp_path
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_live_http_cli_mcp_application_flow(live_org, tmp_path):
    base, tokens, work = live_org
    with urlopen(base + "/org", timeout=10) as response:
        page = response.read().decode()
        assert response.status == 200
        assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert response.headers["X-Frame-Options"] == "DENY"
    assert '/static/org.js' in page and '/static/css/org.css' in page
    with urlopen(base + "/static/org.js", timeout=10) as response:
        assert response.status == 200

    status, project, _ = call(base, "POST", "/api/v1/projects", tokens["admin"], {
        "slug": "live-project", "title": "本地合成项目", "recruiting": True,
    })
    assert status == 201, project
    project_id = project["id"]

    scopes = ["profile:read:self", "projects:read", "knowledge:read",
              "applications:read:self", "applications:create"]
    request_id = secrets.token_urlsafe(24)
    status, authorization, _ = call(base, "POST", "/api/v1/auth/cli/authorization-requests",
                                    tokens["member"], {"request_id": request_id, "scopes": scopes})
    assert status == 200, authorization
    status, exchanged, _ = call(base, "POST", "/api/v1/auth/cli/token",
                                body={"request_id": request_id, "code": authorization["code"]})
    assert status == 200, exchanged
    assert call(base, "POST", "/api/v1/auth/cli/token",
                body={"request_id": request_id, "code": authorization["code"]})[0] == 401

    config_dir = work / "cli-config"
    config_dir.mkdir()
    (config_dir / "credentials.json").write_text(json.dumps({
        "base_url": base, "token": exchanged["token"],
        "expires_at": exchanged["expires_at"], "scopes": scopes,
    }), encoding="utf-8")
    cli_env = dict(os.environ)
    cli_env.update({
        "PYTHONPATH": str(ROOT / "cli"),
        "ALETHIC_API_BASE_URL": base,
        "ALETHIC_CONFIG_DIR": str(config_dir),
    })

    def cli(*arguments):
        result = subprocess.run(
            [sys.executable, "-m", "alethic_cli", *arguments, "--json", "--non-interactive"],
            cwd=ROOT, env=cli_env, capture_output=True, text=True, timeout=20,
        )
        assert exchanged["token"] not in result.stdout + result.stderr
        return result.returncode, json.loads(result.stdout)

    code, listing = cli("projects", "list", "--recruiting")
    assert code == 0 and listing["items"][0]["id"] == project_id
    code, found = cli("knowledge", "search", "成员")
    assert code == 0 and found["items"][0]["id"] == "doc-live"

    application = {"kind": "project-join", "form_revision": "1", "fields": {
        "project_id": project_id,
        "motivation": "参与本地合成项目并验证完整申请闭环",
        "weekly_hours": 4,
        "notes": "仅合成数据",
    }}
    application_path = work / "application.json"
    application_path.write_text(json.dumps(application, ensure_ascii=False), encoding="utf-8")
    code, draft = cli("applications", "draft", "--file", str(application_path))
    assert code == 0 and draft["draft"]["status"] == "draft"
    draft_id = draft["draft"]["id"]
    assert call(base, "POST", f"/api/v1/applications/{draft_id}/confirmations",
                exchanged["token"])[0] == 403
    status, proof, _ = call(base, "POST", f"/api/v1/applications/{draft_id}/confirmations",
                            tokens["member"])
    assert status == 201, proof
    status, submitted, headers = call(base, "POST", "/api/v1/applications", tokens["member"],
                                      {"draft_id": draft_id, "confirmation": proof["confirmation"]},
                                      idempotency="live-e2e-submit")
    assert status == 201 and submitted["status"] == "submitted"
    request_id_header = headers.get("X-Request-ID") or headers.get("x-request-id")
    assert request_id_header and request_id_header.startswith("req_")
    assert call(base, "POST", "/api/v1/applications", tokens["member"],
                {"draft_id": draft_id, "confirmation": proof["confirmation"]},
                idempotency="live-e2e-submit")[1]["status"] == "submitted"

    status, queue, _ = call(base, "GET", "/api/v1/applications?assigned=true", tokens["admin"])
    assert status == 200 and queue["items"][0]["id"] == draft_id
    status, detail, _ = call(base, "GET", f"/api/v1/applications/{draft_id}", tokens["admin"])
    assert status == 200 and detail["preview"]["fields"]["weekly_hours"] == 4
    status, reviewed, _ = call(base, "POST", f"/api/v1/applications/{draft_id}/review",
                               tokens["admin"], {"decision": "approved", "reason": "本地闭环验收"})
    assert status == 200 and reviewed["status"] == "approved"

    wrapper = work / "cli_wrapper.py"
    wrapper.write_text(
        "import runpy,sys\n"
        f"sys.path.insert(0,{str(ROOT / 'cli')!r})\n"
        "sys.argv[0]='alethic'\n"
        "runpy.run_module('alethic_cli',run_name='__main__')\n",
        encoding="utf-8",
    )
    launcher = (
        "import sys; from alethic_mcp.server import CliRunner,serve; "
        f"serve(CliRunner((sys.executable,{str(wrapper)!r}),10))"
    )
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "live-e2e", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "org_search_knowledge", "arguments": {"query": "成员"}}},
    ]
    mcp_env = dict(cli_env)
    mcp_env["PYTHONPATH"] = str(ROOT / "mcp_adapter")
    result = subprocess.run(
        [sys.executable, "-c", launcher], cwd=ROOT, env=mcp_env,
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True, text=True, timeout=30,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert "org_submit_application" not in tool_names
    assert "doc-live" in responses[2]["result"]["content"][0]["text"]
    assert exchanged["token"] not in result.stdout + result.stderr

    status, error, _ = call(base, "POST", "/api/v1/applications/validate", body=application)
    assert status == 401 and error["error"]["code"] == "AUTH_REQUIRED" and error["request_id"]
    status, old_error, _ = call(base, "GET", "/api/auth/me")
    assert status == 401 and set(old_error) == {"detail"}
    assert call(base, "POST", "/api/admin/accounts/member-live/role", tokens["admin"],
                {"role": "applicant"})[0] == 200
    assert cli("projects", "list")[0] == 3


def test_edge_rejects_org_writes(monkeypatch, tmp_path):
    from app import auth, db

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "edge.db")
    monkeypatch.setattr(config, "IS_EDGE", True)
    db.init_db()
    with db.connection() as conn:
        now = auth._utcnow().isoformat()
        conn.execute(
            "INSERT INTO account (student_id,role,pw_hash,pw_changed,created_at,updated_at)"
            " VALUES ('edge-member','member','password',1,?,?)", (now, now),
        )
        token = auth.issue_session(conn, "member", None, "edge-member")
    request = FastAPIRequest({
        "type": "http", "method": "POST", "path": "/api/v1/applications/drafts",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "query_string": b"", "server": ("test", 80), "client": ("test", 123),
        "scheme": "http", "root_path": "", "http_version": "1.1",
    })
    with pytest.raises(Exception) as caught:
        require_org_identity(request)
    assert getattr(caught.value, "status_code", None) == 503
