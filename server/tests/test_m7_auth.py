import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import auth, db
from conftest import applicant_headers, applicant_login

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _app_payload(student_id, name):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "male",
        "class_name": "计科244",
        "qq": "600600600",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "M7 账号体系测试",
    }


def test_applicant_salt(client, cid):
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload("08261001", "盐测试"))
    assert res.status_code == 200
    res = client.get("/api/auth/salt",
                     params={"cid": cid, "student_id": "08261001"})
    assert res.status_code == 200
    salt = res.json()["salt"]
    assert len(salt) == 32  # 16 字节随机盐的 hex
    int(salt, 16)  # 须为合法 hex

    # 无报名记录 → 不区分是否存在：返回确定性伪盐（防账号枚举）
    res = client.get("/api/auth/salt", params={"cid": cid, "student_id": "08261999"})
    assert res.status_code == 200
    pseudo = res.json()["salt"]
    assert len(pseudo) == 32 and pseudo != salt
    int(pseudo, 16)  # 同样为合法 hex
    assert pseudo == auth.pseudo_salt("08261999")  # 确定性


def test_admin_salt(client):
    from app import config
    # 管理员同样走统一 salt（旧 /api/admin/salt 已移除）
    res = client.get("/api/auth/salt", params={"student_id": config.ADMIN_USERNAME})
    assert res.status_code == 200
    assert len(res.json()["salt"]) == 32
    # 不存在的账号同样返回伪盐而非 404
    res = client.get("/api/auth/salt", params={"student_id": "no-such-admin"})
    assert res.status_code == 200
    assert res.json()["salt"] == auth.pseudo_salt("no-such-admin")


def test_applicant_login_success(client, cid):
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload("08261002", "登录成功"))
    data = applicant_login(client, cid, "08261002")
    assert data["student_id"] == "08261002"
    assert data["name"] == "登录成功"
    assert data["pw_changed"] is False
    assert data["token"]


def test_applicant_login_wrong_password_401(client, cid):
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload("08261003", "错密码"))
    res = client.get("/api/auth/salt", params={"cid": cid, "student_id": "08261003"})
    salt = res.json()["salt"]
    wrong = auth.compute_client_hash("wrong-password", salt)
    res = client.post(f"/api/campaigns/{cid}/auth/login",
                      json={"student_id": "08261003", "client_hash": wrong})
    assert res.status_code == 401
    assert res.json()["detail"] == "学号或密码错误"


def test_change_password_invalidates_old_hash(client, cid):
    student = "08261004"
    client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student, "改密"))
    old_token = applicant_login(client, cid, student)["token"]

    res = client.get("/api/auth/salt", params={"cid": cid, "student_id": student})
    old_salt = res.json()["salt"]
    old_hash = auth.compute_client_hash(student, old_salt)
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": auth.compute_client_hash("not-my-password", old_salt),
        "new_salt": auth.new_salt(),
        "new_client_hash": auth.compute_client_hash("anything", auth.new_salt()),
    }, headers={"Authorization": f"Bearer {old_token}"})
    assert res.status_code == 401  # 旧密码不对

    new_salt = auth.new_salt()
    new_hash = auth.compute_client_hash("new-pass-123", new_salt)
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": old_hash,
        "new_salt": new_salt,
        "new_client_hash": new_hash,
    }, headers={"Authorization": f"Bearer {old_token}"})
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}

    # 旧 hash 已失效
    res = client.post(f"/api/campaigns/{cid}/auth/login",
                      json={"student_id": student, "client_hash": old_hash})
    assert res.status_code == 401
    # 新 hash 可登录，且 pw_changed=1
    res = client.post(f"/api/campaigns/{cid}/auth/login",
                      json={"student_id": student, "client_hash": new_hash})
    assert res.status_code == 200
    assert res.json()["pw_changed"] is True


def test_expired_session_invalidated_and_cleaned(client, cid):
    student = "08261005"
    client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student, "过期"))
    data = applicant_login(client, cid, student)
    token = data["token"]
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db.connection() as conn:
        conn.execute("UPDATE login_session SET expires_at = ? WHERE token_hash = ?",
                     (past, token_hash))

    res = client.post("/api/auth/logout",
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401

    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM login_session WHERE token_hash = ?",
                            (token_hash,)).fetchone()["c"] == 0


def test_enroll_requires_login_401_and_mismatch_403(client, cid):
    # 未登录 → 401
    res = client.post(f"/api/campaigns/{cid}/exam/enroll", json={"track": "track1"})
    assert res.status_code == 401

    student = "08261006"
    client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student, "403甲"))
    headers = applicant_headers(client, cid, student)

    # body 带了别人的 student_id → 403
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1", "student_id": "08261099"},
                      headers=headers)
    assert res.status_code == 403

    # body student_id 与会话一致 → 正常开考
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1", "student_id": student},
                      headers=headers)
    assert res.status_code == 200


def test_cross_campaign_session_403(client, cid, admin_headers):
    res = client.post("/api/campaigns", json={"id": "m7-other", "name": "另一批次",
                                              "year": 2026}, headers=admin_headers)
    assert res.status_code in (201, 409)

    student = "08261007"
    client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student, "跨批"))
    headers = applicant_headers(client, cid, student)

    # 用 2026 批次的会话访问 m7-other 批次 → 403
    res = client.post("/api/campaigns/m7-other/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 403


def test_admin_login_and_admin_api_access(client, cid):
    from app import config
    from conftest import ADMIN_TEST_PASSWORD, ensure_admin_password_changed
    # 统一登录：管理员与考生同一端点，靠 role 区分（旧 /api/admin/login 已移除）
    res = client.post("/api/auth/login", json={
        "student_id": config.ADMIN_USERNAME, "client_hash": "bad-hash"})
    assert res.status_code == 401

    # 强制改密门禁：播种管理员初始密码=学号，先收敛为测试密码再验登录
    ensure_admin_password_changed(client)
    res = client.get("/api/auth/salt", params={"student_id": config.ADMIN_USERNAME})
    client_hash = auth.compute_client_hash(ADMIN_TEST_PASSWORD, res.json()["salt"])
    res = client.post("/api/auth/login", json={
        "student_id": config.ADMIN_USERNAME, "client_hash": client_hash})
    assert res.status_code == 200
    assert res.json()["role"] == "admin"
    assert res.json()["pw_changed"] is True
    token = res.json()["token"]

    res = client.get(f"/api/campaigns/{cid}/applications",
                     headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200

    # 登出后会话失效
    res = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    res = client.get(f"/api/campaigns/{cid}/applications",
                     headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401


def test_legacy_admin_token_rejected(client, cid):
    for token in ("dev-admin-token", "test-admin-token", "whatever"):
        res = client.get(f"/api/campaigns/{cid}/applications",
                         headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 401
    # /api/admin/verify 已移除
    assert client.post("/api/admin/verify", json={"token": "dev-admin-token"}).status_code == 404
    assert client.get("/api/admin/verify").status_code == 404
