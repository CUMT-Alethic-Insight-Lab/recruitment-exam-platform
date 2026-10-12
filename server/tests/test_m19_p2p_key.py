"""POST /{cid}/exam/p2p-key：向考生下发短时 Headscale preauthkey 的测试。

未配置 HEADSCALE_API_KEY / LAB_TAILNET_IP 或 Headscale 调用失败时返回 503
（客户端回退 HTTPS 上传）；Headscale HTTP 调用经 monkeypatch 假造。
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import applicant_headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.routes import exam as exam_mod  # noqa: E402


def _app_payload(student_id):
    return {
        "student_id": student_id,
        "name": f"P2P{student_id[-2:]}",
        "gender": "male",
        "class_name": "计科26-1",
        "qq": f"20000{student_id[-3:]}",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "M19 P2P 入网",
    }


def _applicant(client, cid, student_id):
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student_id))
    assert res.status_code == 200
    return applicant_headers(client, cid, student_id)


def _p2p(client, cid, headers=None):
    return client.post(f"/api/campaigns/{cid}/exam/p2p-key", headers=headers or {})


@pytest.fixture
def p2p_configured(monkeypatch):
    monkeypatch.setattr(config, "HEADSCALE_API_KEY", "hs-api-key-test")
    monkeypatch.setattr(config, "LAB_TAILNET_IP", "100.64.0.10")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_urlopen(captured, payload):
    def fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["authorization"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _FakeResp(payload)
    return fake


def test_p2p_key_unavailable_when_unconfigured(client, cid, monkeypatch):
    monkeypatch.setattr(config, "HEADSCALE_API_KEY", "")
    monkeypatch.setattr(config, "LAB_TAILNET_IP", "")
    headers = _applicant(client, cid, "08269701")
    res = _p2p(client, cid, headers)
    assert res.status_code == 503
    assert res.json()["detail"] == "p2p unavailable"


def test_p2p_key_unavailable_without_lab_ip(client, cid, monkeypatch):
    monkeypatch.setattr(config, "HEADSCALE_API_KEY", "hs-api-key-test")
    monkeypatch.setattr(config, "LAB_TAILNET_IP", "")
    headers = _applicant(client, cid, "08269702")
    res = _p2p(client, cid, headers)
    assert res.status_code == 503
    assert res.json()["detail"] == "p2p unavailable"


def test_p2p_key_requires_auth(client, cid):
    res = _p2p(client, cid)
    assert res.status_code == 401


def test_p2p_key_wrong_campaign_forbidden(client, cid):
    headers = _applicant(client, cid, "08269703")
    # require_applicant 先于路由体校验会话绑定的批次
    res = _p2p(client, "m19-other", headers)
    assert res.status_code == 403


def test_p2p_key_happy_path(client, cid, monkeypatch, p2p_configured):
    captured = {}
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen(captured, {"preAuthKey": {"key": "tskey-test-123"}}))
    headers = _applicant(client, cid, "08269704")
    res = _p2p(client, cid, headers)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["login_server"] == config.HEADSCALE_URL
    assert out["auth_key"] == "tskey-test-123"
    assert out["lab_ip"] == "100.64.0.10"
    assert out["receiver_port"] == 8897
    assert out["api_port"] == 8901
    # 发出请求的 URL/头/体与 Headscale REST API 约定一致
    assert captured["url"] == f"{config.HEADSCALE_URL}/api/v1/preauthkey"
    assert captured["authorization"] == "Bearer hs-api-key-test"
    assert captured["timeout"] == 5
    body = captured["body"]
    assert body["user"] == config.HEADSCALE_USER_ID
    assert body["reusable"] is False
    assert body["ephemeral"] is True
    assert body["aclTags"] == ["tag:candidate"]
    assert body["expiration"].endswith("Z")
    # 审计落库
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'exam_p2p_key' AND actor = ?",
            ("08269704",),
        ).fetchone()
    assert row is not None
    assert row["target"] == cid


def test_p2p_key_headscale_timeout_503(client, cid, monkeypatch, p2p_configured):
    def timeout_urlopen(req, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", timeout_urlopen)
    headers = _applicant(client, cid, "08269705")
    res = _p2p(client, cid, headers)
    assert res.status_code == 503
    assert res.json()["detail"] == "p2p unavailable"
    assert "hs-api-key-test" not in res.text


def test_p2p_key_rate_limited(client, cid, monkeypatch, p2p_configured):
    captured = {}
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen(captured, {"preAuthKey": {"key": "tskey-rl"}}))
    monkeypatch.setattr(exam_mod, "P2P_KEY_LIMIT_PER_HOUR", 2)
    headers = _applicant(client, cid, "08269706")
    assert _p2p(client, cid, headers).status_code == 200
    assert _p2p(client, cid, headers).status_code == 200
    res = _p2p(client, cid, headers)
    assert res.status_code == 429
