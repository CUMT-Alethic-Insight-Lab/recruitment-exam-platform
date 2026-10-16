"""POST /{cid}/exam/kit-token：考生换取机试安装包短时签名下载 URL 的测试。

R2 私有 + HMAC 签名 URL（CF Worker 验签）的签发侧：未登录 401 / 未报名 404 /
发放窗口关闭 403 / 每学号 10 次/小时限流（第 11 次 429）/ 签名可离线重放验算 /
每次签发写审计。签名格式见 routes/exam.py 的 sign_kit_url 与
server/deploy/kit-dl-gate（Worker 侧同构实现）。
"""

import hashlib
import hmac
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

from conftest import applicant_headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.routes import exam as exam_mod  # noqa: E402

TEST_SECRET = "kit-signing-secret-test"


def _app_payload(student_id):
    return {
        "student_id": student_id,
        "name": f"KIT{student_id[-2:]}",
        "gender": "male",
        "class_name": "计科26-1",
        "qq": f"20000{student_id[-3:]}",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "机试安装包下载",
    }


def _applicant(client, cid, student_id):
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student_id))
    assert res.status_code == 200
    return applicant_headers(client, cid, student_id)


def _kit_token(client, cid, headers=None):
    return client.post(f"/api/campaigns/{cid}/exam/kit-token", headers=headers or {})


@pytest.fixture
def kit_configured(monkeypatch):
    """测试专用签名密钥 + 清空限流窗口（模块级 dict 跨用例共享，须隔离）。"""
    monkeypatch.setattr(config, "KIT_SIGNING_SECRET", TEST_SECRET)
    exam_mod._kit_token_calls.clear()


def test_kit_token_requires_auth(client, cid):
    res = _kit_token(client, cid)
    assert res.status_code == 401


def test_kit_token_wrong_campaign_forbidden(client, cid):
    headers = _applicant(client, cid, "08268901")
    res = _kit_token(client, "kit-other-campaign", headers)
    assert res.status_code == 403


def test_kit_token_no_application_404(client, cid, kit_configured):
    headers = _applicant(client, cid, "08268902")
    # 模拟「有会话无报名记录」（如现场补录前的边缘态）：报名行直接摘除
    with db.connection() as conn:
        conn.execute(
            "DELETE FROM application WHERE campaign_id = ? AND student_id = ?",
            (cid, "08268902"),
        )
    res = _kit_token(client, cid, headers)
    assert res.status_code == 404
    assert res.json()["detail"] == "未找到该学号的报名记录"


def test_kit_token_window_closed_403(client, cid, monkeypatch):
    monkeypatch.setattr(config, "KIT_DOWNLOAD_OPEN", False)
    headers = _applicant(client, cid, "08268903")
    res = _kit_token(client, cid, headers)
    assert res.status_code == 403
    assert "开放时段" in res.json()["detail"]
    # 窗口关闭时绝不下发任何地址
    assert "sig=" not in res.text


def test_kit_token_unconfigured_secret_503(client, cid, monkeypatch):
    monkeypatch.setattr(config, "KIT_SIGNING_SECRET", "")
    headers = _applicant(client, cid, "08268904")
    res = _kit_token(client, cid, headers)
    assert res.status_code == 503


def test_kit_token_rate_limited_11th_429(client, cid, kit_configured):
    headers = _applicant(client, cid, "08268905")
    for _ in range(10):
        assert _kit_token(client, cid, headers).status_code == 200
    res = _kit_token(client, cid, headers)
    assert res.status_code == 429
    # 被限流的第 11 次不再签发（audit 只有前 10 次）
    with db.connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM audit_log"
            " WHERE action = 'exam_kit_token' AND actor = ?",
            ("08268905",),
        ).fetchone()["c"]
    assert n == 10


def test_kit_token_signature_roundtrip(client, cid, kit_configured):
    headers = _applicant(client, cid, "08268906")
    res = _kit_token(client, cid, headers)
    assert res.status_code == 200, res.text
    out = res.json()
    url = urllib.parse.urlparse(out["url"])
    qs = urllib.parse.parse_qs(url.query)
    exp, sig = int(qs["exp"][0]), qs["sig"][0]
    # 签名重放验算：hex(HMAC_SHA256(secret, "{key}\n{exp}"))，与 Worker 同构
    want = hmac.new(TEST_SECRET.encode(),
                    f"{config.KIT_OBJECT_KEY}\n{exp}".encode(),
                    hashlib.sha256).hexdigest()
    assert sig == want
    assert url.scheme == "https"
    assert url.netloc == urllib.parse.urlparse(config.KIT_DL_BASE).netloc
    assert url.path == f"/{config.KIT_OBJECT_KEY}"
    assert exp == out["expires_at"]
    assert out["ttl_seconds"] == config.KIT_URL_TTL_SECONDS
    assert abs(exp - time.time() - config.KIT_URL_TTL_SECONDS) < 60


def test_kit_token_audit_row_written(client, cid, kit_configured):
    headers = _applicant(client, cid, "08268907")
    res = _kit_token(client, cid, headers)
    assert res.status_code == 200
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'exam_kit_token' AND actor = ?",
            ("08268907",),
        ).fetchone()
    assert row is not None
    assert row["target"] == cid
    exp = res.json()["expires_at"]
    assert row["detail"] == f"kit url signed, exp={exp}"
