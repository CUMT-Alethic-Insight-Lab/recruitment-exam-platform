"""M12 强制首次登录改密：pw_changed=0（初始密码=学号）的会话，
除 me / change_password / logout 外，个人资料类接口（profile/avatar）与管理接口一律 403；
机试场景（require_applicant 的 campaign 级接口）为线下监考，不受门禁影响。
"""

import sys
from pathlib import Path

from app import auth
from conftest import applicant_login, change_password

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PW_DETAIL = "请先修改初始密码"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _register(client, cid, student_id, name="M12考生"):
    res = client.post(f"/api/campaigns/{cid}/applications/register",
                      json={"student_id": student_id, "name": name, "gender": "male"})
    assert res.status_code == 200, res.text


def _unified_login(client, student_id, password=None):
    password = password if password is not None else student_id
    res = client.get("/api/auth/salt", params={"student_id": student_id})
    assert res.status_code == 200, res.text
    client_hash = auth.compute_client_hash(password, res.json()["salt"])
    res = client.post("/api/auth/login",
                      json={"student_id": student_id, "client_hash": client_hash})
    assert res.status_code == 200, res.text
    return res.json()


def test_initial_password_applicant_gated_until_change(client, cid):
    student = "08264301"
    _register(client, cid, student, "门禁考生")
    login = _unified_login(client, student)
    assert login["pw_changed"] is False
    headers = {"Authorization": f"Bearer {login['token']}"}

    # 白名单：me 可用
    res = client.get("/api/auth/me", headers=headers)
    assert res.status_code == 200
    assert res.json()["pw_changed"] is False

    # 个人资料类接口一律 403
    res = client.post("/api/auth/profile", json={"nickname": "想改名"}, headers=headers)
    assert res.status_code == 403
    assert res.json()["detail"] == PW_DETAIL
    res = client.post("/api/auth/avatar",
                      files={"file": ("a.png", PNG_MAGIC + b"\x01" * 20, "image/png")},
                      headers=headers)
    assert res.status_code == 403
    assert res.json()["detail"] == PW_DETAIL

    # 白名单：logout 可用（另签一个会话验证，避免踢掉正在用的）
    second = _unified_login(client, student)
    res = client.post("/api/auth/logout",
                      headers={"Authorization": f"Bearer {second['token']}"})
    assert res.status_code == 200
    res = client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {second['token']}"})
    assert res.status_code == 401

    # 改密后放行
    res = change_password(client, student, student, "NewPw#12345", login["token"])
    assert res.status_code == 200, res.text
    res = client.post("/api/auth/profile", json={"nickname": "想改名"}, headers=headers)
    assert res.status_code == 200, res.text
    res = client.post("/api/auth/avatar",
                      files={"file": ("a.png", PNG_MAGIC + b"\x01" * 20, "image/png")},
                      headers=headers)
    assert res.status_code == 200, res.text


def test_exam_flow_not_gated_for_initial_password(client, cid):
    """边界：机试走 require_applicant 的 campaign 级接口（含 exam_kit 兼容登录），
    初始密码会话照常可用；同一会话访问个人资料类接口仍被拦。"""
    student = "08264302"
    _register(client, cid, student, "机试考生")
    data = applicant_login(client, cid, student)  # exam_kit 同款兼容端点登录
    assert data["pw_changed"] is False
    headers = {"Authorization": f"Bearer {data['token']}"}

    res = client.get(f"/api/campaigns/{cid}/auth/me", headers=headers)
    assert res.status_code == 200, res.text
    res = client.get(f"/api/campaigns/{cid}/exam/questions", headers=headers)
    assert res.status_code == 200, res.text
    res = client.get(f"/api/campaigns/{cid}/applications/{student}", headers=headers)
    assert res.status_code == 200, res.text

    res = client.post("/api/auth/profile", json={"nickname": "机试考生"}, headers=headers)
    assert res.status_code == 403
    assert res.json()["detail"] == PW_DETAIL


def test_initial_password_admin_gated_until_change(client, cid, admin_headers):
    student = "08264303"
    _register(client, cid, student, "新晋管理")
    res = client.post(f"/api/admin/accounts/{student}/role", json={"role": "admin"},
                      headers=admin_headers)
    assert res.status_code == 200, res.text

    login = _unified_login(client, student)
    assert login["role"] == "admin"
    assert login["pw_changed"] is False
    headers = {"Authorization": f"Bearer {login['token']}"}

    # 管理接口（require_admin）一律 403
    for path in ("/api/admin/accounts", "/api/campaigns"):
        res = client.get(path, headers=headers)
        assert res.status_code == 403, f"{path}: {res.text}"
        assert res.json()["detail"] == PW_DETAIL
    res = client.post(f"/api/admin/accounts/{student}/reset_password", headers=headers)
    assert res.status_code == 403
    # set_role 走 accounts 自己的会话校验，同样被门禁拦住
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "applicant"}, headers=headers)
    assert res.status_code == 403

    # 白名单：me 可用；改密后管理接口放行
    res = client.get("/api/auth/me", headers=headers)
    assert res.status_code == 200
    res = change_password(client, student, student, "Admin#12345", login["token"])
    assert res.status_code == 200, res.text
    res = client.get("/api/admin/accounts", headers=headers)
    assert res.status_code == 200, res.text

    # 还原现场（测试共用同一数据库）：降回考生
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "applicant"}, headers=admin_headers)
    assert res.status_code == 200, res.text


def test_reset_password_reinstates_forced_change(client, cid, admin_headers):
    student = "08264304"
    _register(client, cid, student, "重置考生")
    login = _unified_login(client, student)
    res = change_password(client, student, student, "First#12345", login["token"])
    assert res.status_code == 200, res.text
    headers = {"Authorization": f"Bearer {login['token']}"}
    res = client.post("/api/auth/profile", json={"nickname": "已改密"}, headers=headers)
    assert res.status_code == 200, res.text

    # 管理员重置 → 打回初始密码，且该账号全部会话被踢（旧 token 直接 401）
    res = client.post(f"/api/admin/accounts/{student}/reset_password",
                      headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.post("/api/auth/profile", json={"nickname": "再改名"}, headers=headers)
    assert res.status_code == 401

    # 重新登录（默认密码=学号）→ 个人资料类接口再次被门禁拦住
    login = _unified_login(client, student)
    assert login["pw_changed"] is False
    res = client.post("/api/auth/profile", json={"nickname": "再改名"},
                      headers={"Authorization": f"Bearer {login['token']}"})
    assert res.status_code == 403
    assert res.json()["detail"] == PW_DETAIL

    # 重新改密后再次放行
    res = change_password(client, student, student, "Second#12345", login["token"])
    assert res.status_code == 200, res.text
    res = client.post("/api/auth/profile", json={"nickname": "再改名"},
                      headers={"Authorization": f"Bearer {login['token']}"})
    assert res.status_code == 200, res.text
