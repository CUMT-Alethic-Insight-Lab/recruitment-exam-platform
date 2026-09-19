"""M14 账号安全加固：登录限流（login_attempt）、防账号枚举（伪盐+统一 401）、
改密/重置踢旧会话、new_salt 长度与弱密码字典、管理操作审计日志、头像登录可见。
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import auth, config, db
from conftest import applicant_login, change_password

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _register(client, cid, student_id, name="M14考生"):
    res = client.post(f"/api/campaigns/{cid}/applications/register",
                      json={"student_id": student_id, "name": name, "gender": "male"})
    assert res.status_code == 200, res.text


def _unified_login(client, student_id, password=None):
    password = password if password is not None else student_id
    res = client.get("/api/auth/salt", params={"student_id": student_id})
    assert res.status_code == 200, res.text
    client_hash = auth.compute_client_hash(password, res.json()["salt"])
    return client.post("/api/auth/login",
                       json={"student_id": student_id, "client_hash": client_hash})


def _wrong_login(client, student_id, cid=None):
    res = client.get("/api/auth/salt", params={"student_id": student_id})
    assert res.status_code == 200, res.text
    wrong = auth.compute_client_hash("wrong-password", res.json()["salt"])
    url = f"/api/campaigns/{cid}/auth/login" if cid else "/api/auth/login"
    return client.post(url, json={"student_id": student_id, "client_hash": wrong})


# ---------- 登录限流 ----------


def test_login_rate_limit_locks_and_recovers(client, cid):
    student = "08264901"
    _register(client, cid, student, "限流考生")

    # 连续失败 10 次内均为 401，第 10 次触发锁定
    for i in range(auth.LOGIN_FAIL_LIMIT):
        res = _wrong_login(client, student)
        assert res.status_code == 401, f"第 {i + 1} 次: {res.text}"
    # 锁定中：取盐与登录一律 429（直接打端点，取盐本身也会 429）
    res = client.get("/api/auth/salt", params={"student_id": student})
    assert res.status_code == 429
    assert "尝试次数过多" in res.json()["detail"]
    res = client.post("/api/auth/login",
                      json={"student_id": student, "client_hash": "ab" * 32})
    assert res.status_code == 429
    # 正确密码在锁定中同样 429（锁定检查先于口令校验；取盐此时也已 429）
    res = client.post("/api/auth/login",
                      json={"student_id": student, "client_hash": "cd" * 32})
    assert res.status_code == 429

    # 锁定期满后恢复正常，成功登录清零计数
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with db.connection() as conn:
        conn.execute("UPDATE login_attempt SET locked_until = ? WHERE key = ?",
                     (past, student))
    res = _unified_login(client, student)
    assert res.status_code == 200, res.text
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM login_attempt WHERE key = ?",
                            (student,)).fetchone()["c"] == 0


def test_login_success_clears_failure_count(client, cid):
    student = "08264902"
    _register(client, cid, student, "清零考生")
    for _ in range(5):
        assert _wrong_login(client, student).status_code == 401
    assert _unified_login(client, student).status_code == 200
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM login_attempt WHERE key = ?",
                            (student,)).fetchone()["c"] == 0


def test_salt_pseudo_probing_counts_toward_limit(client):
    """对不存在学号反复取伪盐同样计入限流（防刷量），锁定后登录一并 429。"""
    student = "99998888"
    for i in range(auth.LOGIN_FAIL_LIMIT):
        res = client.get("/api/auth/salt", params={"student_id": student})
        assert res.status_code == 200, f"第 {i + 1} 次: {res.text}"
        assert res.json()["salt"] == auth.pseudo_salt(student)
    res = client.get("/api/auth/salt", params={"student_id": student})
    assert res.status_code == 429
    res = client.post("/api/auth/login",
                      json={"student_id": student, "client_hash": "ab" * 32})
    assert res.status_code == 429


def test_compat_login_shares_rate_limit(client, cid):
    """exam_kit 兼容登录端点与统一登录共用同一学号维度的限流计数。"""
    student = "08264903"
    _register(client, cid, student, "兼容限流")
    for _ in range(auth.LOGIN_FAIL_LIMIT):
        res = _wrong_login(client, student, cid=cid)
        assert res.status_code == 401
    res = client.post(f"/api/campaigns/{cid}/auth/login",
                      json={"student_id": student, "client_hash": "ab" * 32})
    assert res.status_code == 429
    res = client.post("/api/auth/login",
                      json={"student_id": student, "client_hash": "ab" * 32})
    assert res.status_code == 429


# ---------- 改密/重置踢旧会话 ----------


def test_change_password_kicks_other_sessions(client, cid):
    student = "08264910"
    _register(client, cid, student, "踢会话考生")
    t1 = _unified_login(client, student).json()["token"]
    t2 = _unified_login(client, student).json()["token"]

    res = change_password(client, student, student, "Kick#12345", t1)
    assert res.status_code == 200, res.text

    # 当前会话保留，其他会话作废
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {t1}"})
    assert res.status_code == 200
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {t2}"})
    assert res.status_code == 401


def test_reset_password_kicks_all_sessions(client, cid, admin_headers):
    student = "08264911"
    _register(client, cid, student, "重置踢人")
    token = applicant_login(client, cid, student)["token"]

    res = client.post(f"/api/admin/accounts/{student}/reset_password",
                      headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401


# ---------- new_salt 长度与弱密码字典 ----------


def test_change_password_salt_length_and_weak_dict(client, cid):
    student = "08264912"
    _register(client, cid, student, "弱密码考生")
    token = _unified_login(client, student).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    old_hash = auth.compute_client_hash(
        student, client.get("/api/auth/salt", params={"student_id": student}).json()["salt"])

    # 空盐 / 短盐（< 32 位 hex）→ 400
    for bad_salt in ("", "ab" * 8, "ab" * 15):
        res = client.post("/api/auth/change_password", json={
            "old_client_hash": old_hash, "new_salt": bad_salt,
            "new_client_hash": "cd" * 32}, headers=headers)
        assert res.status_code == 400, f"salt={bad_salt!r}: {res.text}"

    # 常见弱密码（字典命中）→ 400
    new_salt = auth.new_salt()
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": old_hash, "new_salt": new_salt,
        "new_client_hash": auth.compute_client_hash("123456", new_salt)}, headers=headers)
    assert res.status_code == 400
    assert "常见" in res.json()["detail"]

    # 正常强度 → 200
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": old_hash, "new_salt": new_salt,
        "new_client_hash": auth.compute_client_hash("Str0ng#Pass9", new_salt)},
        headers=headers)
    assert res.status_code == 200, res.text


# ---------- 管理操作审计日志 ----------


def test_admin_audit_log(client, cid, admin_headers):
    student = "08264913"
    _register(client, cid, student, "审计考生")

    res = client.post(f"/api/admin/accounts/{student}/role", json={"role": "admin"},
                      headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.post(f"/api/admin/accounts/{student}/reset_password",
                      headers=admin_headers)
    assert res.status_code == 200, res.text

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE target = ? ORDER BY id", (student,)
        ).fetchall()
    assert [r["action"] for r in rows] == ["set_role", "reset_password"]
    assert rows[0]["actor"] == config.ADMIN_USERNAME
    assert rows[0]["detail"] == "applicant -> admin"
    assert rows[1]["actor"] == config.ADMIN_USERNAME
    assert all(r["created_at"] for r in rows)

    # 只读查询端点：管理员可见（倒序），未登录 401
    res = client.get("/api/admin/audit_log", headers=admin_headers)
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) >= 2
    assert items[0]["action"] == "reset_password"  # 最新在前
    assert client.get("/api/admin/audit_log").status_code == 401

    # 还原现场：降回考生
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "applicant"}, headers=admin_headers)
    assert res.status_code == 200, res.text


# ---------- 头像登录可见（不限本人） ----------


def test_avatar_requires_login_but_not_self(client, cid):
    owner, viewer = "08264920", "08264921"
    _register(client, cid, owner, "头像主")
    _register(client, cid, viewer, "旁观者")
    owner_token = _unified_login(client, owner).json()["token"]
    viewer_token = _unified_login(client, viewer).json()["token"]

    png = PNG_MAGIC + b"\x00\x00\x00\x0dIHDR" + b"\x01" * 20
    # 初始密码账号先改密再上传（强制改密门禁）
    res = change_password(client, owner, owner, "AvO#12345", owner_token)
    assert res.status_code == 200, res.text
    res = client.post("/api/auth/avatar", files={"file": ("a.png", png, "image/png")},
                      headers={"Authorization": f"Bearer {owner_token}"})
    assert res.status_code == 200, res.text

    # 未登录 → 401；其他登录用户可读（不限本人）
    res = client.get("/api/auth/avatar", params={"student_id": owner})
    assert res.status_code == 401
    res = client.get("/api/auth/avatar", params={"student_id": owner},
                     headers={"Authorization": f"Bearer {viewer_token}"})
    assert res.status_code == 200
    assert res.content == png
