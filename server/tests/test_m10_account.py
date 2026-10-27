"""M10 账号体系：account 表迁移播种、统一登录、昵称/头像/改密校验、后台账号管理。"""

import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import auth, config, db
from app.studentid import parse_student_id
from conftest import ADMIN_TEST_PASSWORD, admin_login, applicant_headers, change_password, ensure_admin_password_changed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _register(client: TestClient, cid: str, student_id: str,
              name="M10考生", gender="male"):
    return client.post(f"/api/campaigns/{cid}/applications/register",
                       json={"student_id": student_id, "name": name, "gender": gender})


def _unified_login(client: TestClient, student_id: str, password: str | None = None):
    password = password if password is not None else student_id
    res = client.get("/api/auth/salt", params={"student_id": student_id})
    assert res.status_code == 200, res.text
    client_hash = auth.compute_client_hash(password, res.json()["salt"])
    return client.post("/api/auth/login",
                       json={"student_id": student_id, "client_hash": client_hash})


def _make_applicant(client: TestClient, cid: str, student_id: str,
                    name="M10考生", gender="male"):
    res = _register(client, cid, student_id, name, gender)
    assert res.status_code == 200, res.text
    return _unified_login(client, student_id).json()


def _admin_session(client: TestClient, student_id: str, password: str | None = None):
    """以 admin 角色账号走统一 /api/auth/login，返回 Bearer headers（kind=admin 会话）。
    新提升的管理员 pw_changed=0，按强制改密流程先改密再返回。"""
    password = password if password is not None else student_id
    res = client.get("/api/auth/salt", params={"student_id": student_id})
    assert res.status_code == 200, res.text
    client_hash = auth.compute_client_hash(password, res.json()["salt"])
    res = client.post("/api/auth/login",
                      json={"student_id": student_id, "client_hash": client_hash})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "admin"
    token = res.json()["token"]
    if not res.json()["pw_changed"]:
        res = change_password(client, student_id, password, f"Pw#{student_id}", token)
        assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {token}"}


# ---------- 迁移播种 ----------


def test_migration_seeds_account_from_legacy(tmp_path):
    """application/admin_user → account：考生灌入（昵称=姓名）、同号时管理员凭据优先、幂等。"""
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    now = "2026-01-01T00:00:00+00:00"

    app_salt = auth.new_salt()
    app_hash = auth.store_hash(auth.compute_client_hash("08264201", app_salt))
    conn.execute(
        "INSERT INTO application (student_id, campaign_id, name, gender, class_name, qq,"
        " lab_intent, domains, focus_next_year, pw_salt, pw_hash, pw_changed,"
        " created_at, updated_at)"
        " VALUES ('08264201', '2026', '迁移考生', 'male', '计科244', '600600600',"
        " 1, '[]', 'project', ?, ?, 0, ?, ?)",
        (app_salt, app_hash, now, now),
    )
    admin_salt = auth.new_salt()
    admin_hash = auth.store_hash(auth.compute_client_hash("08264299", admin_salt))
    conn.execute(
        "INSERT INTO admin_user (username, pw_salt, pw_hash, pw_changed, created_at)"
        " VALUES ('08264299', ?, ?, 0, ?)",
        (admin_salt, admin_hash, now),
    )
    # 同号双身份：08264201 既是考生也是管理员 → 管理员凭据/角色优先
    dup_salt = auth.new_salt()
    dup_hash = auth.store_hash(auth.compute_client_hash("08264201", dup_salt))
    conn.execute(
        "INSERT INTO admin_user (username, pw_salt, pw_hash, pw_changed, created_at)"
        " VALUES ('08264201', ?, ?, 1, ?)",
        (dup_salt, dup_hash, now),
    )

    db._migrate_account(conn)
    rows = {r["student_id"]: r for r in conn.execute("SELECT * FROM account")}
    assert rows["08264201"]["role"] == "admin"
    assert rows["08264201"]["pw_hash"] == dup_hash
    assert rows["08264201"]["pw_changed"] == 1
    assert rows["08264299"]["role"] == "admin"
    applicant_sid = "08264202"
    conn.execute(
        "INSERT INTO application (student_id, campaign_id, name, gender, class_name, qq,"
        " lab_intent, domains, focus_next_year, pw_salt, pw_hash, pw_changed,"
        " created_at, updated_at)"
        " VALUES (?, '2026', '另一考生', 'female', '计科244', '600600601',"
        " 0, '[]', 'project', NULL, NULL, 0, ?, ?)",
        (applicant_sid, now, now),
    )
    db._migrate_account(conn)
    rows = {r["student_id"]: r for r in conn.execute("SELECT * FROM account")}
    assert rows[applicant_sid]["role"] == "applicant"
    assert rows[applicant_sid]["nickname"] == "另一考生"

    # 幂等：已有 account 数据时重跑不覆盖（用户改过的昵称/密码不被回滚）
    conn.execute("UPDATE account SET nickname = '改过的昵称' WHERE student_id = '08264201'")
    conn.execute(
        "INSERT OR IGNORE INTO application (student_id, campaign_id, name, gender,"
        " class_name, qq, lab_intent, domains, focus_next_year, created_at, updated_at)"
        " VALUES ('08264203', '2026', '迟到考生', 'male', '计科244', '600600602',"
        " 0, '[]', 'project', ?, ?)",
        (now, now),
    )
    db._migrate_account(conn)
    rows = {r["student_id"]: r for r in conn.execute("SELECT * FROM account")}
    assert rows["08264201"]["nickname"] == "改过的昵称"
    assert rows["08264203"]["nickname"] == "迟到考生"  # 新增考生仍会补种
    conn.close()


def test_seed_admin_creates_account_admin(client):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM account WHERE student_id = ?", (config.ADMIN_USERNAME,)
        ).fetchone()
    assert row is not None
    assert row["role"] == "admin"


# ---------- 统一登录三分支 ----------


def test_unified_login_admin_branch(client):
    ensure_admin_password_changed(client)  # 强制改密后管理员密码 = 测试密码
    res = client.get("/api/auth/salt", params={"student_id": config.ADMIN_USERNAME})
    assert res.status_code == 200
    client_hash = auth.compute_client_hash(ADMIN_TEST_PASSWORD, res.json()["salt"])
    res = client.post("/api/auth/login", json={
        "student_id": config.ADMIN_USERNAME, "client_hash": client_hash})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["role"] == "admin"
    assert data["student_id"] == config.ADMIN_USERNAME
    assert data["name"] == ""
    assert "nickname" in data
    assert data["has_avatar"] is False
    assert isinstance(data["pw_changed"], bool)
    assert "cid" not in data and "registration_complete" not in data
    assert data["token"]

    # /api/auth/me 与登录响应同形态
    res = client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {data['token']}"})
    assert res.status_code == 200, res.text
    me = res.json()
    assert me["role"] == "admin"
    assert "cid" not in me and "registration_complete" not in me


def test_unified_login_applicant_branch(client, cid):
    data = _make_applicant(client, cid, "08264100", "统一登录考生")
    assert data["role"] == "applicant"
    assert data["student_id"] == "08264100"
    assert data["name"] == "统一登录考生"
    assert data["nickname"] == "统一登录考生"  # 建号时昵称默认 = 姓名
    assert data["cid"] == cid
    assert data["registration_complete"] is False
    assert data["pw_changed"] is False
    assert data["has_avatar"] is False

    res = client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {data['token']}"})
    me = res.json()
    assert me["role"] == "applicant"
    assert me["cid"] == cid
    assert me["registration_complete"] is False
    assert me["name"] == "统一登录考生"


def test_unified_login_unknown_account_masked(client):
    """防账号枚举：不存在学号的 salt 返回伪盐、登录与密码错误同为 401。"""
    res = client.get("/api/auth/salt", params={"student_id": "99999999"})
    assert res.status_code == 200
    assert res.json()["salt"] == auth.pseudo_salt("99999999")
    res = client.post("/api/auth/login",
                      json={"student_id": "99999999", "client_hash": "ab" * 32})
    assert res.status_code == 401
    assert res.json()["detail"] == "学号或密码错误"


def test_unified_login_wrong_password_401(client, cid):
    _make_applicant(client, cid, "08264101", "错密码甲")
    res = client.get("/api/auth/salt", params={"student_id": "08264101"})
    wrong = auth.compute_client_hash("not-my-password", res.json()["salt"])
    res = client.post("/api/auth/login",
                      json={"student_id": "08264101", "client_hash": wrong})
    assert res.status_code == 401
    assert res.json()["detail"] == "学号或密码错误"


def test_salt_cid_compat_ignored(client, cid):
    """exam_kit 兼容：带 cid 的 salt 请求照常工作，cid 被忽略。"""
    _make_applicant(client, cid, "08264102", "兼容考生")
    with_cid = client.get("/api/auth/salt",
                          params={"cid": "2026", "student_id": "08264102"})
    without = client.get("/api/auth/salt", params={"student_id": "08264102"})
    assert with_cid.status_code == 200 and without.status_code == 200
    assert with_cid.json()["salt"] == without.json()["salt"]


# ---------- 昵称 ----------


def test_profile_nickname_validation(client, cid):
    data = _make_applicant(client, cid, "08264103", "昵称考生")
    # 强制改密门禁：初始密码账号须先改密才能用个人资料类接口
    res = change_password(client, "08264103", "08264103", "NicK#12345", data["token"])
    assert res.status_code == 200, res.text
    headers = {"Authorization": f"Bearer {data['token']}"}

    # 合法：中文/字母/数字/中间空格/_/-/·
    for ok in ("小明", "Nick_01", "名字·甲", "a-b", "Nick Name 01", "汉Mix_2-x·y"):
        res = client.post("/api/auth/profile", json={"nickname": ok}, headers=headers)
        assert res.status_code == 200, f"{ok!r}: {res.text}"
        assert res.json() == {"ok": True, "nickname": ok}

    # 非法：空、超长、特殊字符、控制字符
    for bad in ("", "   ", "a" * 17, "<script>", "昵称!", "a\nb", "a\tb", "昵/称"):
        res = client.post("/api/auth/profile", json={"nickname": bad}, headers=headers)
        assert res.status_code == 400, f"{bad!r}: {res.text}"

    # trim 后落库：首尾空格被去除
    res = client.post("/api/auth/profile", json={"nickname": "  收尾空格  "},
                      headers=headers)
    assert res.status_code == 200
    assert res.json()["nickname"] == "收尾空格"


def test_profile_requires_login(client):
    assert client.post("/api/auth/profile", json={"nickname": "匿名"}).status_code == 401


# ---------- 头像 ----------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"


def _webp_bytes() -> bytes:
    return b"RIFF\x24\x00\x00\x00WEBPVP8 \x10\x00\x00\x00" + b"\x00" * 16


def test_avatar_upload_read_and_magic_rejection(client, cid):
    data = _make_applicant(client, cid, "08264104", "头像考生")
    # 强制改密门禁：初始密码账号须先改密才能上传头像
    res = change_password(client, "08264104", "08264104", "Ava#12345", data["token"])
    assert res.status_code == 200, res.text
    headers = {"Authorization": f"Bearer {data['token']}"}

    # 未登录读头像 → 401（头像登录可见，防匿名按学号遍历）
    res = client.get("/api/auth/avatar", params={"student_id": "08264104"})
    assert res.status_code == 401

    # 未上传 → 404
    res = client.get("/api/auth/avatar", params={"student_id": "08264104"},
                     headers=headers)
    assert res.status_code == 404

    # PNG 正常上传 + 读取
    png = PNG_MAGIC + b"\x00\x00\x00\x0dIHDR" + b"\x01" * 20
    res = client.post("/api/auth/avatar", files={"file": ("a.png", png, "image/png")},
                      headers=headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "has_avatar": True}
    res = client.get("/api/auth/avatar", params={"student_id": "08264104"},
                     headers=headers)
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.content == png

    # JPEG / WebP 可上传；换格式后旧文件被删，读取为新类型
    jpeg = JPEG_MAGIC + b"\x01" * 20
    res = client.post("/api/auth/avatar", files={"file": ("b.jpg", jpeg, "image/jpeg")},
                      headers=headers)
    assert res.status_code == 200
    res = client.get("/api/auth/avatar", params={"student_id": "08264104"},
                     headers=headers)
    assert res.headers["content-type"] == "image/jpeg" and res.content == jpeg
    webp = _webp_bytes()
    res = client.post("/api/auth/avatar", files={"file": ("c.webp", webp, "image/webp")},
                      headers=headers)
    assert res.status_code == 200
    res = client.get("/api/auth/avatar", params={"student_id": "08264104"},
                     headers=headers)
    assert res.headers["content-type"] == "image/webp" and res.content == webp

    # 魔数不符 → 400（SVG / HTML / 随机字节）
    for bad in (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                b"<html><body>x</body></html>",
                b"\x00\x01\x02\x03not-an-image"):
        res = client.post("/api/auth/avatar",
                          files={"file": ("x.png", bad, "image/png")}, headers=headers)
        assert res.status_code == 400, f"{bad[:12]!r}: {res.text}"

    # 超过 2MB → 400
    too_big = PNG_MAGIC + b"0" * (2 * 1024 * 1024)
    res = client.post("/api/auth/avatar",
                      files={"file": ("big.png", too_big, "image/png")}, headers=headers)
    assert res.status_code == 400

    # 未登录 → 401
    res = client.post("/api/auth/avatar",
                      files={"file": ("a.png", png, "image/png")})
    assert res.status_code == 401


# ---------- 改密 ----------


def test_change_password_rejections_and_success(client, cid):
    student = "08264105"
    _make_applicant(client, cid, student, "改密考生")
    headers = applicant_headers(client, cid, student)
    res = client.get("/api/auth/salt", params={"student_id": student})
    old_salt = res.json()["salt"]
    old_hash = auth.compute_client_hash(student, old_salt)

    new_salt = auth.new_salt()

    # ① 非法 hex / 长度 ≠ 64
    for bad_hash in ("zz" * 32, "abc", "12345" * 12 + "1" * 4 + "g"):
        res = client.post("/api/auth/change_password", json={
            "old_client_hash": old_hash, "new_salt": new_salt,
            "new_client_hash": bad_hash}, headers=headers)
        assert res.status_code == 400, f"{bad_hash[:8]!r}: {res.text}"
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": old_hash, "new_salt": "nothex",
        "new_client_hash": "ab" * 32}, headers=headers)
    assert res.status_code == 400

    # ② 新密码 == 学号（默认密码）
    default_hash = auth.compute_client_hash(student, new_salt)
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": old_hash, "new_salt": new_salt,
        "new_client_hash": default_hash}, headers=headers)
    assert res.status_code == 400

    # ③ 新密码 == 旧密码
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": old_hash, "new_salt": new_salt,
        "new_client_hash": old_hash}, headers=headers)
    assert res.status_code == 400

    # 原密码错误 → 401
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": auth.compute_client_hash("wrong", old_salt),
        "new_salt": new_salt,
        "new_client_hash": auth.compute_client_hash("NewPass123", new_salt)},
        headers=headers)
    assert res.status_code == 401

    # 成功改密
    good_hash = auth.compute_client_hash("NewPass123", new_salt)
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": old_hash, "new_salt": new_salt,
        "new_client_hash": good_hash}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}

    # 旧密码失效，新密码可登录且 pw_changed=True
    res = client.post("/api/auth/login",
                      json={"student_id": student, "client_hash": old_hash})
    assert res.status_code == 401
    res = client.post("/api/auth/login",
                      json={"student_id": student, "client_hash": good_hash})
    assert res.status_code == 200
    assert res.json()["pw_changed"] is True


# ---------- 账号列表 / 角色 / 重置密码 ----------


def test_admin_accounts_list_and_fuzzy_query(client, cid, admin_headers):
    _make_applicant(client, cid, "08264110", "名单甲")
    _make_applicant(client, cid, "08264111", "名单乙")

    res = client.get("/api/admin/accounts", headers=admin_headers)
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) >= 3
    mine = next(i for i in items if i["student_id"] == "08264110")
    assert mine["name"] == "名单甲"
    assert mine["nickname"] == "名单甲"
    assert mine["role"] == "applicant"
    assert mine["dept"] == "08"
    assert mine["enroll_year"] == 2026
    assert mine["pw_changed"] is False
    assert mine["registration_complete"] is False
    assert mine["created_at"]
    admin_item = next(i for i in items if i["student_id"] == config.ADMIN_USERNAME)
    assert admin_item["role"] == "admin"

    # 按学号 / 姓名 / 昵称模糊匹配
    for q in ("08264110", "826411", "名单甲", "单甲"):
        res = client.get("/api/admin/accounts", params={"q": q}, headers=admin_headers)
        ids = [i["student_id"] for i in res.json()["items"]]
        assert "08264110" in ids, f"q={q}: {ids}"
    res = client.get("/api/admin/accounts", params={"q": "绝不存在的关键字xyz"},
                     headers=admin_headers)
    assert res.json()["items"] == []

    # 按角色筛选
    res = client.get("/api/admin/accounts", params={"role": "admin"},
                     headers=admin_headers)
    assert all(i["role"] == "admin" for i in res.json()["items"])
    res = client.get("/api/admin/accounts", params={"role": "applicant"},
                     headers=admin_headers)
    assert all(i["role"] == "applicant" for i in res.json()["items"])

    # 未登录 → 401
    assert client.get("/api/admin/accounts").status_code == 401


def test_role_change_and_protections(client, cid, admin_headers):
    a, b, c = "08264120", "08264121", "08264122"
    for sid, name in ((a, "角色甲"), (b, "角色乙"), (c, "角色丙")):
        _make_applicant(client, cid, sid, name)

    # 非法角色 → 400
    res = client.post(f"/api/admin/accounts/{a}/role", json={"role": "mentor"},
                      headers=admin_headers)
    assert res.status_code == 400
    # 账号不存在 → 404
    res = client.post("/api/admin/accounts/99999999/role", json={"role": "admin"},
                      headers=admin_headers)
    assert res.status_code == 404

    # 提升甲为管理员（此时 2 名管理员），甲取得 kind=admin 会话
    res = client.post(f"/api/admin/accounts/{a}/role", json={"role": "admin"},
                      headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}
    a_headers = _admin_session(client, a)

    # 甲降级自己 → 400（自降级保护）
    res = client.post(f"/api/admin/accounts/{a}/role", json={"role": "applicant"},
                      headers=a_headers)
    assert res.status_code == 400

    # 甲降级播种管理员 → 200（尚有甲一名管理员，非最后一个）
    res = client.post(f"/api/admin/accounts/{config.ADMIN_USERNAME}/role",
                      json={"role": "applicant"}, headers=a_headers)
    assert res.status_code == 200, res.text

    # 此时只剩甲：降级自己 → 400（最后管理员保护）
    res = client.post(f"/api/admin/accounts/{a}/role", json={"role": "applicant"},
                      headers=a_headers)
    assert res.status_code == 400

    # 甲提升乙、乙提升丙后，乙可降级甲（非最后一个管理员）
    client.post(f"/api/admin/accounts/{b}/role", json={"role": "admin"},
                headers=a_headers)
    b_headers = _admin_session(client, b)
    res = client.post(f"/api/admin/accounts/{a}/role", json={"role": "applicant"},
                      headers=b_headers)
    assert res.status_code == 200, res.text

    with db.connection() as conn:
        roles = {r["student_id"]: r["role"] for r in
                 conn.execute("SELECT student_id, role FROM account")}
    assert roles[a] == "applicant"
    assert roles[b] == "admin"

    # 恢复现场（测试共用同一数据库）：播种管理员复位，乙降回考生
    res = client.post(f"/api/admin/accounts/{config.ADMIN_USERNAME}/role",
                      json={"role": "admin"}, headers=b_headers)
    assert res.status_code == 200, res.text
    restored_headers = {"Authorization": f"Bearer {admin_login(client).json()['token']}"}
    admin_headers.update(restored_headers)
    res = client.post(f"/api/admin/accounts/{b}/role", json={"role": "applicant"},
                      headers=restored_headers)
    assert res.status_code == 200, res.text
    with db.connection() as conn:
        roles = {r["student_id"]: r["role"] for r in
                 conn.execute("SELECT student_id, role FROM account")}
    assert roles[config.ADMIN_USERNAME] == "admin"
    assert roles[b] == "applicant"


def test_reset_password_restores_default_login(client, cid, admin_headers):
    student = "08264130"
    _make_applicant(client, cid, student, "重置考生")
    headers = applicant_headers(client, cid, student)

    # 先改成新密码
    res = client.get("/api/auth/salt", params={"student_id": student})
    old_salt = res.json()["salt"]
    new_salt = auth.new_salt()
    new_hash = auth.compute_client_hash("ResetPass9", new_salt)
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": auth.compute_client_hash(student, old_salt),
        "new_salt": new_salt, "new_client_hash": new_hash}, headers=headers)
    assert res.status_code == 200, res.text

    # 管理员重置 → 默认密码（学号）可登录，pw_changed 归零
    res = client.post(f"/api/admin/accounts/{student}/reset_password",
                      headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}

    res = client.post("/api/auth/login",
                      json={"student_id": student, "client_hash": new_hash})
    assert res.status_code == 401  # 新密码已失效
    res = _unified_login(client, student)
    assert res.status_code == 200
    assert res.json()["pw_changed"] is False

    # 账号不存在 → 404
    res = client.post("/api/admin/accounts/99999999/reset_password",
                      headers=admin_headers)
    assert res.status_code == 404
    # 未登录 → 401
    assert client.post(f"/api/admin/accounts/{student}/reset_password").status_code == 401


# ---------- 学号解析 ----------


def test_parse_student_id():
    parsed = parse_student_id("08261234")
    assert parsed == {"valid": True, "dept": "08", "enroll_year": 2026, "serial": "1234"}
    assert parse_student_id("11259999") == {
        "valid": True, "dept": "11", "enroll_year": 2025, "serial": "9999"}
    for bad in ("", "0826123", "082612345", "abcd1234", "0826-123", "0826 234", None, 8261234):
        assert parse_student_id(bad)["valid"] is False, bad
