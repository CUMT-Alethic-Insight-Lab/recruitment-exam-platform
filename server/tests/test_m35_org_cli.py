import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


CLI_ROOT = Path(__file__).resolve().parents[2] / "cli"
sys.path.insert(0, str(CLI_ROOT))
from alethic_cli import cli
from alethic_cli.client import CliError, normalize_base
from alethic_cli.credentials import CredentialStore


TOKEN = "private-cli-bearer-never-print"
CODE = "one-time-code-never-print"


@pytest.fixture
def api(monkeypatch, tmp_path):
    requests = []
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            requests.append({"method": self.command, "path": self.path, "body": body, "auth": self.headers.get("Authorization")})
            path = urlsplit(self.path).path
            response = routes.get((self.command, path), (200, {"items": [], "next_cursor": None}))
            if callable(response):
                response = response(requests[-1])
            status, value = response
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if status == 302:
                self.send_header("Location", "/recruitment/api/v1/leaked")
            self.end_headers()
            self.wfile.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/recruitment"
    monkeypatch.setenv("ALETHIC_API_BASE_URL", base)
    monkeypatch.setenv("ALETHIC_CONFIG_DIR", str(tmp_path / "config"))
    store = CredentialStore()
    store.directory.mkdir()
    store.path.write_text(json.dumps({"base_url": base, "token": TOKEN, "expires_at": time.time() + 300, "scopes": ["projects:read", "applications:create"]}), encoding="utf-8")
    yield {"base": base, "routes": routes, "requests": requests, "store": store, "tmp": tmp_path}
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def invoke(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main([*args, "--json"])
    raw = stdout.getvalue()
    value = json.loads(raw)
    assert len(raw.splitlines()) == 1
    assert TOKEN not in raw + stderr.getvalue()
    assert CODE not in raw + stderr.getvalue()
    return code, value, stderr.getvalue()


@pytest.mark.parametrize("arguments,path,query", [
    (["capabilities"], "/capabilities", {}),
    (["auth", "status"], "/me", {}),
    (["projects", "list", "--recruiting", "--limit", "5", "--cursor", "a+b"], "/projects", {"limit": ["5"], "cursor": ["a+b"], "recruiting": ["true"]}),
    (["projects", "get", "project/a"], "/projects/project%2Fa", {}),
    (["knowledge", "search", "项目 & rules"], "/knowledge/search", {"q": ["项目 & rules"], "limit": ["20"]}),
    (["knowledge", "get", "doc/a"], "/knowledge/documents/doc%2Fa", {}),
    (["forms", "schema"], "/forms/project-join/schema", {}),
    (["forms", "schema", "project-join"], "/forms/project-join/schema", {}),
    (["applications", "list", "--project", "p1"], "/applications", {"mine": ["true"], "project": ["p1"], "limit": ["20"]}),
    (["applications", "get", "a1"], "/applications/a1", {}),
])
def test_commands_make_real_http_requests(api, arguments, path, query):
    code, _, _ = invoke(*arguments)
    assert code == 0
    request = api["requests"][-1]
    parsed = urlsplit(request["path"])
    assert parsed.path == "/recruitment/api/v1" + path
    assert parse_qs(parsed.query) == query
    assert request["auth"] == "Bearer " + TOKEN


@pytest.mark.parametrize("status,expected,code", [
    (400, 6, "VALIDATION_FAILED"), (401, 3, "AUTH_REQUIRED"),
    (403, 4, "RESOURCE_FORBIDDEN"), (404, 4, "RESOURCE_FORBIDDEN"),
    (409, 7, "STATE_CONFLICT"), (422, 6, "VALIDATION_FAILED"),
    (428, 7, "STATE_CONFLICT"), (429, 5, "API_UNAVAILABLE"),
    (500, 5, "API_UNAVAILABLE"), (503, 5, "API_UNAVAILABLE"),
])
def test_http_errors_are_stable_and_do_not_echo_secrets(api, status, expected, code):
    api["routes"][("GET", "/recruitment/api/v1/capabilities")] = (status, {"error": {"code": TOKEN, "message": CODE, "details": {"token": TOKEN}}})
    actual, value, _ = invoke("capabilities")
    assert actual == expected
    assert value["error"]["code"] == code
    assert value["error"]["http_status"] == status


def test_preserves_allowed_server_error_code(api):
    api["routes"][("GET", "/recruitment/api/v1/capabilities")] = (403, {"error": {"code": "INSUFFICIENT_SCOPE", "message": TOKEN}})
    code, value, _ = invoke("capabilities")
    assert code == 4
    assert value["error"]["code"] == "INSUFFICIENT_SCOPE"


@pytest.mark.parametrize("expiry", [0, "2000-01-01T00:00:00Z", "not-a-date", None, True])
def test_expired_or_invalid_credentials_never_reach_api(api, expiry):
    stored = json.loads(api["store"].path.read_text())
    stored["expires_at"] = expiry
    api["store"].path.write_text(json.dumps(stored))
    code, value, _ = invoke("capabilities", "--non-interactive")
    assert code == 3
    assert value["error"]["code"] == "AUTH_REQUIRED"
    assert api["requests"] == []


def test_credentials_are_bound_to_base_url(api):
    stored = json.loads(api["store"].path.read_text())
    stored["base_url"] = "https://other.example"
    api["store"].path.write_text(json.dumps(stored))
    assert invoke("capabilities")[0] == 3
    assert api["requests"] == []


def test_login_uses_web_then_public_code_exchange_and_private_store(api, monkeypatch):
    opened = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(cli.getpass, "getpass", lambda *args, **kwargs: CODE)
    expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)).isoformat()
    api["routes"][("POST", "/recruitment/api/v1/auth/cli/token")] = (200, {"token": TOKEN, "expires_at": expiry, "scopes": ["projects:read"]})
    code, result, stderr = invoke("auth", "login", "--scope", "projects:read")
    assert code == 0 and result["authenticated"] is True
    url = urlsplit(opened[0])
    query = parse_qs(url.query)
    assert url.path == "/recruitment/org"
    assert len(query["cli_request"][0]) == 32
    assert query["scopes"] == ["projects:read"]
    assert opened[0] in stderr
    request = api["requests"][-1]
    assert request["body"] == {"request_id": query["cli_request"][0], "code": CODE}
    assert request["auth"] is None
    assert api["store"].load(api["base"])["token"] == TOKEN
    if os.name != "nt":
        assert stat.S_IMODE(api["store"].path.stat().st_mode) == 0o600
        assert stat.S_IMODE(api["store"].directory.stat().st_mode) == 0o700


def test_noninteractive_login_does_not_open_browser_or_prompt(api, monkeypatch):
    monkeypatch.setattr(cli.webbrowser, "open", lambda _: pytest.fail("browser must not open"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda *args, **kwargs: pytest.fail("must not prompt"))
    code, value, _ = invoke("--non-interactive", "auth", "login")
    assert code == 3
    assert value["error"]["code"] == "AUTH_REQUIRED"
    assert value["error"]["authorization_url"].startswith(api["base"] + "/org?")
    assert api["requests"] == []


def test_scopes_local_and_logout_truthfully_reports_no_server_revoke(api):
    assert invoke("auth", "scopes")[1]["scopes"] == ["projects:read", "applications:create"]
    assert api["requests"] == []
    code, value, stderr = invoke("auth", "logout")
    assert code == 0 and value == {"logged_out": True, "server_revoked": False}
    assert "NOT revoked" in stderr
    assert not api["store"].path.exists()
    assert invoke("capabilities")[0] == 3
    assert invoke("auth", "logout")[0] == 0


def application_file(api, value=None):
    value = value if value is not None else {"kind": "project-join", "form_revision": "1", "fields": {"project_id": "p1", "motivation": "参与项目"}}
    path = api["tmp"] / "application.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path), value


def test_dry_run_calls_only_server_validation(api):
    filename, body = application_file(api)
    api["routes"][("POST", "/recruitment/api/v1/applications/validate")] = (200, {"valid": True, "normalized": body, "confirmation_token": "must-not-print"})
    code, result, _ = invoke("applications", "submit", "--file", filename, "--dry-run")
    assert code == 0 and result["valid"] is True
    assert "confirmation_token" not in result
    assert len(api["requests"]) == 1
    assert api["requests"][0]["path"].endswith("/applications/validate")
    assert api["requests"][0]["body"] == body


@pytest.mark.parametrize("action,exit_code", [("draft", 0), ("submit", 7)])
def test_draft_and_submit_require_trusted_web_confirmation(api, action, exit_code):
    filename, body = application_file(api)
    api["routes"][("POST", "/recruitment/api/v1/applications/drafts")] = (201, {"id": "draft/1", "status": "draft", "confirmation_token": "secret-confirmation", "confirmation_url": "https://untrusted.invalid"})
    code, result, _ = invoke("applications", action, "--file", filename)
    assert code == exit_code
    if action == "submit":
        assert result["error"]["code"] == "CONFIRMATION_REQUIRED"
        result = result["error"]
    assert result["confirmation_url"] == api["base"] + "/org?draft_id=draft%2F1"
    assert result["submitted"] is False
    assert "confirmation_token" not in result["draft"]
    assert len(api["requests"]) == 1
    assert api["requests"][0]["path"].endswith("/applications/drafts")
    assert api["requests"][0]["body"] == body


@pytest.mark.parametrize("value,expected", [([], 6), ({"confirmation": {"confirmation_token": "fake"}}, 7), ({"fields": {"nested": [{"confirmation_token": "fake"}]}}, 7)])
def test_invalid_and_confirmation_bearing_files_are_rejected_locally(api, value, expected):
    filename, _ = application_file(api, value)
    assert invoke("applications", "submit", "--file", filename)[0] == expected
    assert api["requests"] == []


def test_invalid_json_and_unknown_confirm_flag(api):
    filename, _ = application_file(api)
    Path(filename).write_text("not json")
    assert invoke("applications", "draft", "--file", filename)[0] == 6
    code, value, _ = invoke("applications", "submit", "--file", filename, "--confirm")
    assert code == 2 and value["error"]["code"] == "INVALID_ARGUMENT"
    assert api["requests"] == []


@pytest.mark.parametrize("response", [(200, b"not json"), (302, {}), (200, None)])
def test_invalid_responses_and_redirects_do_not_leak_bearer(api, response):
    api["routes"][("GET", "/recruitment/api/v1/capabilities")] = response
    assert invoke("capabilities")[0] == 5
    assert len(api["requests"]) == 1


def test_output_redacts_nested_credentials_and_bearer_echo(api):
    api["routes"][("GET", "/recruitment/api/v1/capabilities")] = (200, {"token": TOKEN, "nested": [{"confirmation_token": CODE, "description": "echo " + TOKEN}], "capabilities": ["read"]})
    code, value, _ = invoke("capabilities")
    assert code == 0
    assert value == {"nested": [{"description": "echo [REDACTED]"}], "capabilities": ["read"]}


def test_executable_module_json_contract(api):
    result = subprocess.run([sys.executable, "-m", "alethic_cli", "--json", "capabilities"], cwd=CLI_ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"items": [], "next_cursor": None}
    assert TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com?token=bad", "file:///tmp/test"])
def test_rejects_untrusted_or_credential_bearing_base_urls(url):
    with pytest.raises(CliError) as exc:
        normalize_base(url)
    assert exc.value.exit_code == 2


def test_limit_bounds_fail_before_http(api):
    assert invoke("projects", "list", "--limit", "51")[0] == 2
    assert api["requests"] == []


def test_validation_false_is_exit_six(api):
    filename, _ = application_file(api)
    api["routes"][("POST", "/recruitment/api/v1/applications/validate")] = (200, {"valid": False})
    assert invoke("applications", "submit", "--file", filename, "--dry-run")[0] == 6


def test_credential_acl_failure_never_persists_new_token(api, monkeypatch):
    import alethic_cli.credentials as credentials

    api["store"].delete()
    monkeypatch.setattr(credentials, "private_path", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("failure")))
    with pytest.raises(CliError) as exc:
        api["store"].save(api["base"], {"token": TOKEN, "expires_at": time.time() + 300, "scopes": []})
    assert exc.value.exit_code == 5
    assert not api["store"].path.exists()


def test_windows_private_acl_uses_current_user_without_logging_token(monkeypatch, tmp_path):
    import types
    import alethic_cli.credentials as credentials

    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return types.SimpleNamespace(stdout="DOMAIN\\member\n")

    monkeypatch.setattr(credentials, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(credentials.subprocess, "run", run)
    credentials.private_path(tmp_path / "credentials.json")
    assert commands == [["whoami"], ["icacls", str(tmp_path / "credentials.json"), "/inheritance:r", "/grant:r", "DOMAIN\\member:F"]]


def test_posix_permissions_are_private(monkeypatch):
    import types
    import alethic_cli.credentials as credentials

    modes = []
    path = types.SimpleNamespace(chmod=modes.append)
    monkeypatch.setattr(credentials, "os", types.SimpleNamespace(name="posix"))
    credentials.private_path(path)
    credentials.private_path(path, directory=True)
    assert modes == [0o600, 0o700]


def test_non_tty_login_never_prompts(api, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.webbrowser, "open", lambda _: pytest.fail("must not open"))
    code, value, _ = invoke("auth", "login")
    assert code == 3 and value["error"]["code"] == "AUTH_REQUIRED"
    assert api["requests"] == []


@pytest.mark.parametrize("body", [b'{"number": NaN}', b'{"number": 1e999}'])
def test_nonfinite_api_json_is_stable_error(api, body):
    api["routes"][("GET", "/recruitment/api/v1/capabilities")] = (200, body)
    code, value, _ = invoke("capabilities")
    assert code == 5 and value["error"]["code"] == "API_UNAVAILABLE"
