"""考试方式扩展契约测试：exam_mode 入库回显、auth/me、公开题列表、
自选题报名、own_project 可 enroll（开考二选一）、仓库直提端点（multipart）、上传带 repo_url。"""
import sys

from app.constants import EXAM_MODES
from conftest import applicant_headers

sys.path.insert(0, ".")


def _app_payload(student_id, name, exam_mode="standard", overwrite=False):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "female",
        "class_name": "计科244",
        "qq": "600600601",
        "lab_intent": False,
        "domains": ["llm_arch"],
        "focus_next_year": "research",
        "expectation": "考试方式扩展测试",
        "exam_mode": exam_mode,
        "overwrite": overwrite,
    }


def _create_question(client, admin_headers, title, track="track1",
                     domain="llm_arch", active=True):
    res = client.post("/api/questions", json={
        "domain": domain, "track": track, "title": title,
        "body": f"{title} 的题面内容", "active": active,
    }, headers=admin_headers)
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_exam_modes_constant():
    assert EXAM_MODES == {
        "standard": "标准机试",
        "own_project": "个人项目（GitHub 仓库直提）",
        "free_creation": "领域内自由发挥（会话记录 + GitHub 链接）",
    }


def test_exam_mode_stored_and_echoed(client, cid, admin_headers):
    # 默认 standard
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload("08263001", "默认标准"))
    assert res.status_code == 200, res.text
    assert res.json()["exam_mode"] == "standard"

    # 显式选择各方式均可入库并回显
    for i, mode in enumerate(("standard", "own_project", "free_creation"), start=1):
        student = f"0826310{i}"
        res = client.post(f"/api/campaigns/{cid}/applications",
                          json=_app_payload(student, f"方式{i}", exam_mode=mode))
        assert res.status_code == 200, res.text
        assert res.json()["exam_mode"] == mode

    # 非法 exam_mode → 422
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload("08263201", "非法方式", exam_mode="whatever"))
    assert res.status_code == 422

    # overwrite 重新提交可更新 exam_mode（08263001 最初为 standard）
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload("08263001", "默认标准",
                                        exam_mode="free_creation", overwrite=True))
    assert res.status_code == 200, res.text
    assert res.json()["exam_mode"] == "free_creation"


def test_auth_me_contract(client, cid):
    student = "08263401"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "查我"))
    headers = applicant_headers(client, cid, student)

    res = client.get(f"/api/campaigns/{cid}/auth/me", headers=headers)
    assert res.status_code == 200, res.text
    me = res.json()
    assert me["student_id"] == student
    assert me["name"] == "查我"
    assert me["status"] == "applied"
    assert me["exam_mode"] == "standard"
    assert me["has_session"] is False
    assert me["track"] is None

    # 开考后 has_session=True，track 为会话轨迹
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200, res.text
    res = client.get(f"/api/campaigns/{cid}/auth/me", headers=headers)
    assert res.json()["has_session"] is True
    assert res.json()["track"] == "track1"

    # 未登录 → 401
    assert client.get(f"/api/campaigns/{cid}/auth/me").status_code == 401


def test_public_questions_no_meta_leak(client, cid, admin_headers):
    student = "08263501"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "看题"))
    headers = applicant_headers(client, cid, student)

    active_ids = {_create_question(client, admin_headers, "公开题甲"),
                  _create_question(client, admin_headers, "公开题乙", domain="agent",
                                   track="track2")}
    inactive_id = _create_question(client, admin_headers, "隐藏题丙", active=False)

    res = client.get(f"/api/campaigns/{cid}/exam/questions", headers=headers)
    assert res.status_code == 200, res.text
    questions = res.json()
    by_id = {q["id"]: q for q in questions}
    # 只下发 active=1 的题目
    for qid in active_ids:
        assert qid in by_id, qid
    assert inactive_id not in by_id
    # 仅题面五字段，绝不带 meta/评审字段
    for q in questions:
        assert set(q.keys()) == {"id", "domain", "track", "title", "body"}
        assert "meta" not in q and "tags" not in q and "active" not in q
        for word in ("评审专用", "出题人备注", "锚点清单", "评分参考"):
            assert word not in q["body"]

    # 未登录 → 401
    assert client.get(f"/api/campaigns/{cid}/exam/questions").status_code == 401


def test_enroll_with_question_ids(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "自选题一", domain="memory",
                          track="track1")
    q2 = _create_question(client, admin_headers, "自选题二", domain="infra",
                          track="track1")
    q_inactive = _create_question(client, admin_headers, "自选题停用", active=False)

    student = "08263601"
    # 自选领域校验已推广到所有 track：报名领域须覆盖所选题目领域
    client.post(f"/api/campaigns/{cid}/applications",
                json={**_app_payload(student, "自选题"),
                      "domains": ["llm_arch", "memory", "infra"]})
    headers = applicant_headers(client, cid, student)

    # 先覆盖全部非法分支（此时尚无会话，不会撞 409 already_enrolled）
    # 题目不存在 → 400
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1",
                            "question_ids": ["nonexistent0001"]},
                      headers=headers)
    assert res.status_code == 400

    # 含 inactive 题 → 400
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1", "question_ids": [q_inactive]},
                      headers=headers)
    assert res.status_code == 400

    # 超过 3 道 → 400
    more = [_create_question(client, admin_headers, f"超量{i}", domain="llm_arch",
                             track="track1") for i in range(4)]
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1", "question_ids": more},
                      headers=headers)
    assert res.status_code == 400

    # 0 道（空列表）→ 400
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1", "question_ids": []},
                      headers=headers)
    assert res.status_code == 400

    # 合法自选：按提交顺序保序下发（放在最后，成功后即有 draft 会话）
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1", "question_ids": [q1, q2]},
                      headers=headers)
    assert res.status_code == 200, res.text
    served = res.json()["questions"]
    assert [q["id"] for q in served] == [q1, q2]
    for q in served:
        assert set(q.keys()) == {"id", "domain", "title", "body"}

    # 不给 question_ids 走原随机逻辑
    student2 = "08263602"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student2, "随机题"))
    headers2 = applicant_headers(client, cid, student2)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers2)
    assert res.status_code == 200, res.text


def test_own_project_can_enroll(client, cid):
    # 开考二选一：问卷选了 own_project 的考生也允许做题（enroll 不再拦截）
    student = "08263701"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "项目轨", exam_mode="own_project"))
    headers = applicant_headers(client, cid, student)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["track"] == "track1"


def test_project_submit_happy_path(client, cid, admin_headers):
    student = "08263801"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "直提甲", exam_mode="own_project"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/applications/me/project",
                      data={"repo_url": "https://github.com/alethic/demo"},
                      headers=headers)
    assert res.status_code == 200, res.text
    session = res.json()
    assert session["track"] == "project"
    assert session["status"] == "uploaded"
    assert session["machine_id"] == "web"
    assert session["questions_served"] == []
    assert session["repo_url"] == "https://github.com/alethic/demo"
    assert session["attachment_file_path"] is None
    assert session["uploaded_at"]

    # application.status → tested
    res = client.get(f"/api/campaigns/{cid}/applications/{student}", headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["status"] == "tested"

    # auth/me 反映 project 会话
    res = client.get(f"/api/campaigns/{cid}/auth/me", headers=headers)
    assert res.json()["has_session"] is True
    assert res.json()["track"] == "project"


def test_project_submit_allows_standard(client, cid):
    # 开考二选一：standard 考生也可直提自有项目（不再限 own_project）
    student = "08263901"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "标准乙", exam_mode="standard"))
    headers = applicant_headers(client, cid, student)
    res = client.post(f"/api/campaigns/{cid}/applications/me/project",
                      data={"repo_url": "https://github.com/alethic/x"},
                      headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["track"] == "project"


def test_project_submit_duplicate_409(client, cid):
    student = "08263902"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "直提乙", exam_mode="own_project"))
    headers = applicant_headers(client, cid, student)
    res = client.post(f"/api/campaigns/{cid}/applications/me/project",
                      data={"repo_url": "https://github.com/a/b"}, headers=headers)
    assert res.status_code == 200
    res = client.post(f"/api/campaigns/{cid}/applications/me/project",
                      data={"repo_url": "https://github.com/c/d"}, headers=headers)
    assert res.status_code == 409


def test_project_submit_repo_url_validation(client, cid):
    student = "08263903"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "直提丙", exam_mode="own_project"))
    headers = applicant_headers(client, cid, student)
    for bad in ("ftp://github.com/x", "github.com/y"):
        res = client.post(f"/api/campaigns/{cid}/applications/me/project",
                          data={"repo_url": bad}, headers=headers)
        assert res.status_code == 400, bad
    # 空字符串表单字段被 FastAPI 视为缺失 → 422（同样拒绝）
    res = client.post(f"/api/campaigns/{cid}/applications/me/project",
                      data={"repo_url": ""}, headers=headers)
    assert res.status_code == 422


def test_upload_still_accepts_repo_url(client, cid, admin_headers):
    student = "08263904"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "上传丙"))
    headers = applicant_headers(client, cid, student)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200, res.text
    session_id = res.json()["session_id"]

    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": session_id,
              "repo_url": "https://github.com/alethic/override"},
        files={"cleaned": ("cleaned.jsonl", b'{"role":"user","content":"hi"}\n')},
        headers={"X-Session-Token": res.json()["session_token"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["repo_url"] == "https://github.com/alethic/override"
    assert body["status"] == "uploaded"
    res = client.get(f"/api/campaigns/{cid}/applications/{student}", headers=admin_headers)
    assert res.json()["status"] == "tested"
