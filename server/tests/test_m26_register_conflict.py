"""M26 注册冲突覆盖漏洞：学号已存在账号时，注册/报名端点必须 409 拒绝顶号；
问卷补填（未完成续填、本人覆盖更新）等合法路径保持畅通。"""

from conftest import applicant_login

CONFLICT_MSG = "该学号已注册，请直接登录或联系管理员重置密码"


def _register(client, cid, student_id, name="冲突甲", gender="male"):
    return client.post(f"/api/campaigns/{cid}/applications/register", json={
        "student_id": student_id, "name": name, "gender": gender})


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
        "expectation": "M26 冲突测试",
    }


def test_register_admin_account_conflict_409(client, cid, admin_headers):
    """管理员账号（有账号、无本批次报名行）不得被匿名注册顶建档案。"""
    admin_id = "00000000"  # 测试环境播种管理员（见 conftest/config）
    res = _register(client, cid, admin_id, "顶号者")
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == CONFLICT_MSG
    # 未产生管理员报名档案
    res = client.get(f"/api/campaigns/{cid}/applications", headers=admin_headers)
    assert all(item["student_id"] != admin_id for item in res.json())


def test_oneshot_submit_admin_account_conflict_409(client, cid, admin_headers):
    """绕过 register 直接匿名提交完整问卷，同样不得为已有账号建档案。"""
    admin_id = "00000000"
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload(admin_id, "顶号者"))
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == CONFLICT_MSG
    res = client.get(f"/api/campaigns/{cid}/applications", headers=admin_headers)
    assert all(item["student_id"] != admin_id for item in res.json())


def test_cross_campaign_account_conflict_409(client, cid, admin_headers):
    """同账号在另一批次报名：有账号即冲突（不因本批次无报名行而放行）。"""
    res = client.post("/api/campaigns",
                      json={"id": "m26b", "name": "M26 第二批次", "year": 2026},
                      headers=admin_headers)
    assert res.status_code in (201, 409)

    student = "08262601"
    assert _register(client, cid, student, "跨批甲").status_code == 200

    res = _register(client, "m26b", student, "跨批乙")
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == CONFLICT_MSG
    res = client.post(f"/api/campaigns/m26b/applications",
                      json=_app_payload(student, "跨批乙"))
    assert res.status_code == 409, res.text
    # 原批次数据不受影响
    res = client.get(f"/api/campaigns/{cid}/applications/{student}",
                     headers=admin_headers)
    assert res.status_code == 200 and res.json()["name"] == "跨批甲"


def test_completed_overwrite_requires_owner(client, cid):
    """已完成注册的报名：匿名/管理员/他人 token 一律拒绝，仅本人可覆盖更新。"""
    student = "08262602"
    assert _register(client, cid, student, "本人乙").status_code == 200
    # 未完成 → 匿名 overwrite 仍是合法的问卷补全路径
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json={**_app_payload(student, "本人乙"), "overwrite": True})
    assert res.status_code == 200, res.text
    assert res.json()["registration_complete"] == 1

    payload = {**_app_payload(student, "本人乙改"), "overwrite": True}
    # 匿名 → 401
    res = client.post(f"/api/campaigns/{cid}/applications", json=payload)
    assert res.status_code == 401, res.text
    # 他人考生 → 403
    other = applicant_login(client, cid, "08261234")
    res = client.post(f"/api/campaigns/{cid}/applications", json=payload,
                      headers={"Authorization": f"Bearer {other['token']}"})
    assert res.status_code == 403, res.text
    # 本人 → 200 且数据真实更新
    own = applicant_login(client, cid, student)
    res = client.post(f"/api/campaigns/{cid}/applications", json=payload,
                      headers={"Authorization": f"Bearer {own['token']}"})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "本人乙改"
    res = client.get(f"/api/campaigns/{cid}/applications/{student}",
                     headers={"Authorization": f"Bearer {own['token']}"})
    assert res.json()["name"] == "本人乙改"


def test_completed_overwrite_rejects_admin_token(client, cid, admin_headers):
    student = "08262603"
    assert _register(client, cid, student, "本人丙").status_code == 200
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json={**_app_payload(student, "本人丙"), "overwrite": True})
    assert res.status_code == 200, res.text
    # 已完成后再覆盖：管理员会话不算本人
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json={**_app_payload(student, "被管理员改"), "overwrite": True},
                      headers=admin_headers)
    assert res.status_code == 401, res.text


def test_resume_and_fresh_paths_unchanged(client, cid):
    """合法路径回归：首次注册/直提匿名可通，未完成续填幂等更新。"""
    fresh = "08262604"
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload(fresh, "直提丁"))
    assert res.status_code == 200, res.text

    resume = "08262605"
    assert _register(client, cid, resume, "初名").status_code == 200
    res = _register(client, cid, resume, "改 名", "female")
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "改 名"
    assert res.json()["registration_complete"] == 0
