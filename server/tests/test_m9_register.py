"""M9 流程式注册：第一步注册接口 + registration_complete 门槛。"""

from conftest import applicant_login


def _register(client, cid, student_id, name="注册甲", gender="male"):
    return client.post(f"/api/campaigns/{cid}/applications/register", json={
        "student_id": student_id,
        "name": name,
        "gender": gender,
    })


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
        "expectation": "M9 注册流程测试",
    }


def test_register_creates_account_and_login(client, cid):
    student = "08262001"
    res = _register(client, cid, student, "注册甲", "female")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["student_id"] == student
    assert data["campaign_id"] == cid
    assert data["registration_complete"] == 0
    assert "初始密码" in data["message"] and "学号" in data["message"]

    # 建号后可立即用学号作为密码登录
    login = applicant_login(client, cid, student, password=student)
    assert login["student_id"] == student
    assert login["pw_changed"] is False
    own = {"Authorization": f"Bearer {login['token']}"}

    # 存量字段为空默认值
    res = client.get(f"/api/campaigns/{cid}/applications/{student}", headers=own)
    assert res.status_code == 200
    app = res.json()
    assert app["class_name"] == ""
    assert app["qq"] == ""
    assert app["lab_intent"] is False
    assert app["domains"] == []
    assert app["focus_next_year"] == ""
    assert app["expectation"] == ""
    assert app["exam_mode"] == "standard"
    assert app["status"] == "applied"
    assert app["registration_complete"] == 0


def test_register_incomplete_idempotent_continue(client, cid, admin_headers):
    student = "08262002"
    assert _register(client, cid, student, "初次名").status_code == 200
    # 未完成时重复注册 → 幂等续填，更新 name/gender，仍为 0
    res = _register(client, cid, student, "更新名", "female")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["name"] == "更新名"
    assert data["gender"] == "female"
    assert data["registration_complete"] == 0
    res = client.get(f"/api/campaigns/{cid}/applications/{student}",
                     headers=admin_headers)
    assert res.json()["name"] == "更新名"


def test_register_completed_conflict_409(client, cid):
    student = "08262003"
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student, "已完成"))
    assert res.status_code == 200, res.text
    res = _register(client, cid, student)
    assert res.status_code == 409
    assert res.json()["detail"] == "该学号已完成注册，请直接登录"


def test_full_submit_sets_registration_complete(client, cid):
    # 覆盖路径：先注册（0），再完整提交问卷（含 overwrite）→ 1
    student = "08262004"
    assert _register(client, cid, student, "问卷甲").status_code == 200
    res = client.post(
        f"/api/campaigns/{cid}/applications",
        json={**_app_payload(student, "问卷甲"), "overwrite": True},
    )
    assert res.status_code == 200, res.text
    assert res.json()["registration_complete"] == 1
    # 再次覆盖提交，仍为 1
    res = client.post(
        f"/api/campaigns/{cid}/applications",
        json={**_app_payload(student, "问卷乙"), "overwrite": True},
    )
    assert res.status_code == 200, res.text
    assert res.json()["registration_complete"] == 1

    # 首次直接完整提交（非 overwrite 路径）→ 1
    student2 = "08262005"
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student2, "直提"))
    assert res.status_code == 200, res.text
    assert res.json()["registration_complete"] == 1


def test_incomplete_blocked_enroll_and_project(client, cid):
    student = "08262006"
    assert _register(client, cid, student).status_code == 200
    login = applicant_login(client, cid, student, password=student)
    headers = {"Authorization": f"Bearer {login['token']}"}

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 400
    assert res.json()["detail"] == "请先完成报名问卷"

    res = client.post(f"/api/campaigns/{cid}/applications/me/project",
                      json={"repo_url": "https://github.com/a/b"}, headers=headers)
    assert res.status_code == 400
    assert res.json()["detail"] == "请先完成报名问卷"

    # 完成问卷后解除拦截
    res = client.post(
        f"/api/campaigns/{cid}/applications",
        json={**_app_payload(student, "解锁"), "overwrite": True},
    )
    assert res.status_code == 200, res.text
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200, res.text


def test_auth_me_registration_complete_field(client, cid):
    student = "08262007"
    assert _register(client, cid, student).status_code == 200
    login = applicant_login(client, cid, student, password=student)
    headers = {"Authorization": f"Bearer {login['token']}"}
    res = client.get(f"/api/campaigns/{cid}/auth/me", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["registration_complete"] == 0

    res = client.post(
        f"/api/campaigns/{cid}/applications",
        json={**_app_payload(student, "字段"), "overwrite": True},
    )
    assert res.status_code == 200, res.text
    res = client.get(f"/api/campaigns/{cid}/auth/me", headers=headers)
    assert res.json()["registration_complete"] == 1


def test_register_format_validation(client, cid):
    # 学号格式不符 → 422（正则已放宽为任意 8 位数字，"12345678" 属合法）
    for bad in ("0826123", "082612345", "abcd1234", "0826-123"):
        res = _register(client, cid, bad)
        assert res.status_code == 422, f"{bad}: {res.text}"
    # gender 仅收 male/female
    res = client.post(f"/api/campaigns/{cid}/applications/register", json={
        "student_id": "08262008", "name": "性别校验", "gender": "other"})
    assert res.status_code == 422
    # name 不能为空
    res = client.post(f"/api/campaigns/{cid}/applications/register", json={
        "student_id": "08262009", "name": "  ", "gender": "male"})
    assert res.status_code == 422


def test_application_read_permission(client, cid, admin_headers):
    """档案读取：匿名 401、他人 403、本人 200、管理员 200。"""
    from conftest import applicant_login

    mine, other = "08262011", "08262012"
    assert _register(client, cid, mine).status_code == 200
    assert _register(client, cid, other).status_code == 200
    mine_login = applicant_login(client, cid, mine, password=mine)
    other_login = applicant_login(client, cid, other, password=other)
    mine_h = {"Authorization": f"Bearer {mine_login['token']}"}
    other_h = {"Authorization": f"Bearer {other_login['token']}"}

    url = f"/api/campaigns/{cid}/applications"
    # 匿名 → 401
    assert client.get(f"{url}/{mine}").status_code == 401
    # 他人考生 → 403
    assert client.get(f"{url}/{mine}", headers=other_h).status_code == 403
    # 本人 → 200
    res = client.get(f"{url}/{mine}", headers=mine_h)
    assert res.status_code == 200 and res.json()["student_id"] == mine
    # 管理员 → 200
    res = client.get(f"{url}/{mine}", headers=admin_headers)
    assert res.status_code == 200 and res.json()["student_id"] == mine
