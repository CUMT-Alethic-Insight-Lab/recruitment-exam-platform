"""M16 图形验证码：签发、一次性校验、过期/重复拒绝、限流、接口接入。"""

import base64

import pytest

from app import captcha as captcha_mod
from app import config, db


def _read_code(captcha_id: str) -> str:
    with db.connection() as conn:
        return conn.execute(
            "SELECT code FROM captcha WHERE id = ?", (captcha_id,)
        ).fetchone()["code"]


@pytest.fixture(autouse=True)
def _ensure_db():
    """直连 db 的单元用例不经过 client 的 lifespan：逐用例确保表已建（幂等）。"""
    db.init_db()
    yield


def _new_code_captcha() -> tuple[str, str]:
    captcha_id, _png = captcha_mod.new_captcha()
    return captcha_id, _read_code(captcha_id)


@pytest.fixture
def enabled(monkeypatch):
    """conftest 全局关闭验证码；开启态用例逐个局部打开。"""
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", True)


# ---------- 单元：签发与一次性校验 ----------

def test_new_captcha_stores_code_and_returns_png():
    captcha_id, png = captcha_mod.new_captcha()
    assert png.startswith(b"\x89PNG")
    assert len(_read_code(captcha_id)) == captcha_mod.CODE_LENGTH
    # 字符集不含易混字符
    assert set(_read_code(captcha_id)) <= set(captcha_mod.CODE_ALPHABET)


def test_verify_success_is_one_shot():
    captcha_id, code = _new_code_captcha()
    assert captcha_mod.verify_captcha(captcha_id, code) is True
    # 大小写不敏感、容忍首尾空白
    captcha_id, code = _new_code_captcha()
    assert captcha_mod.verify_captcha(captcha_id, " " + code.upper() + " ") is True
    # 一次性：同一记录第二次必拒
    assert captcha_mod.verify_captcha(captcha_id, code) is False


def test_verify_rejects_wrong_missing_expired():
    captcha_id, code = _new_code_captcha()
    assert captcha_mod.verify_captcha(captcha_id, "zzzz") is False
    assert captcha_mod.verify_captcha("no-such-id", code) is False
    assert captcha_mod.verify_captcha("", code) is False
    assert captcha_mod.verify_captcha(captcha_id, "") is False
    assert captcha_mod.verify_captcha(None, None) is False
    # 过期记录一律拒绝
    with db.connection() as conn:
        conn.execute(
            "UPDATE captcha SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (captcha_id,),
        )
    assert captcha_mod.verify_captcha(captcha_id, code) is False
    # 无论对错都消耗：错一次后，同记录的正确码也作废
    captcha_id, code = _new_code_captcha()
    assert captcha_mod.verify_captcha(captcha_id, "aaaa") is False
    assert captcha_mod.verify_captcha(captcha_id, code) is False


# ---------- 签发路由 ----------

def test_issue_captcha_route(client):
    res = client.get("/api/captcha")
    assert res.status_code == 200
    data = res.json()
    assert set(data) == {"id", "image"}
    assert data["image"].startswith("data:image/png;base64,")
    raw = base64.b64decode(data["image"].split(",", 1)[1])
    assert raw.startswith(b"\x89PNG")
    assert _read_code(data["id"])


# ---------- 接口接入：注册（POST /api/campaigns/{cid}/applications/register） ----------

def _register(client, cid, student_id, captcha_id="", captcha_text=""):
    return client.post(
        f"/api/campaigns/{cid}/applications/register",
        json={
            "student_id": student_id,
            "name": "验证码测试",
            "gender": "male",
            "captcha_id": captcha_id,
            "captcha_text": captcha_text,
        },
    )


def test_register_requires_captcha_when_enabled(client, cid, enabled):
    res = _register(client, cid, "08260091")
    assert res.status_code == 400
    assert "验证码" in res.json()["detail"]
    captcha_id, code = _new_code_captcha()
    res = _register(client, cid, "08260091", captcha_id, "xxxx")
    assert res.status_code == 400
    # 错码已消耗记录：与前端一致，失败后换新码再提交
    captcha_id, code = _new_code_captcha()
    res = _register(client, cid, "08260091", captcha_id, code)
    assert res.status_code == 200, res.text


def test_register_passes_without_captcha_when_disabled(client, cid, monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    res = _register(client, cid, "08260092")
    assert res.status_code == 200, res.text


# ---------- 接口接入：问卷提交（POST /api/campaigns/{cid}/applications） ----------

def _submit(client, cid, student_id, captcha_id="", captcha_text=""):
    return client.post(
        f"/api/campaigns/{cid}/applications",
        json={
            "student_id": student_id,
            "name": "验证码测试",
            "gender": "male",
            "class_name": "计科244",
            "qq": "100000001",
            "lab_intent": True,
            "domains": ["agent"],
            "focus_next_year": "project",
            "captcha_id": captcha_id,
            "captcha_text": captcha_text,
        },
    )


def test_submit_application_requires_captcha_when_enabled(client, cid, enabled):
    res = _submit(client, cid, "08260093")
    assert res.status_code == 400
    assert "验证码" in res.json()["detail"]
    captcha_id, code = _new_code_captcha()
    res = _submit(client, cid, "08260093", captcha_id, "xxxx")
    assert res.status_code == 400
    # 错码已消耗记录：与前端一致，失败后换新码再提交
    captcha_id, code = _new_code_captcha()
    res = _submit(client, cid, "08260093", captcha_id, code)
    assert res.status_code == 200, res.text


def test_submit_application_passes_when_disabled(client, cid, monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    res = _submit(client, cid, "08260094")
    assert res.status_code == 200, res.text


# ---------- 签发限流（每 IP 每小时 30 次） ----------

def test_issue_rate_limit(client, monkeypatch):
    # 清掉本 IP 的历史窗口并把限额调小，便于触发
    with db.connection() as conn:
        captcha_mod.clear_issue_rate(conn, "testclient")
    monkeypatch.setattr(captcha_mod, "ISSUE_LIMIT_PER_HOUR", 3)
    responses = [client.get("/api/captcha") for _ in range(3)]
    assert all(r.status_code == 200 for r in responses)
    limited = client.get("/api/captcha")
    assert limited.status_code == 429
    assert "频繁" in limited.json()["detail"]
    # 清理：不污染同 IP 的后续用例与一小时内重跑
    with db.connection() as conn:
        captcha_mod.clear_issue_rate(conn, "testclient")


def test_issue_rate_keyed_by_forwarded_ip(client, monkeypatch):
    """签发限流按真实客户端 IP（XFF 最左）：反代后各用户额度互不干扰。"""
    monkeypatch.setattr(captcha_mod, "ISSUE_LIMIT_PER_HOUR", 2)
    with db.connection() as conn:
        captcha_mod.clear_issue_rate(conn, "10.0.0.1")
        captcha_mod.clear_issue_rate(conn, "10.0.0.2")
    h1 = {"X-Forwarded-For": "10.0.0.1, 127.0.0.1"}
    h2 = {"X-Forwarded-For": "10.0.0.2"}
    assert client.get("/api/captcha", headers=h1).status_code == 200
    assert client.get("/api/captcha", headers=h1).status_code == 200
    assert client.get("/api/captcha", headers=h1).status_code == 429
    # 一个 IP 被限不影响其他 IP
    assert client.get("/api/captcha", headers=h2).status_code == 200
    # 清理：不污染同键的后续用例与一小时内重跑
    with db.connection() as conn:
        captcha_mod.clear_issue_rate(conn, "10.0.0.1")
        captcha_mod.clear_issue_rate(conn, "10.0.0.2")
