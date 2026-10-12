"""开考二选一（track 缺省 = 题库自选）与自有项目 multipart 直提：

- enroll track 缺省/空串必须带 question_ids（否则 400 未选择作答方式），
  会话 track='selected'，domain_tags = 所选题目领域去重；
- 凡 question_ids 自选（不论 track）领域 ⊆ 报名问卷领域，违反 403；
- selected 会话 reroll 400；
- submit_project 改 multipart：repo_url Form + 可选 zip 附件落盘并写库，
  非 zip 400、超 50MB 413。
"""
import sys
from pathlib import Path

import pytest
from conftest import applicant_headers

sys.path.insert(0, ".")

# 本文件在题库创建的题目 id，按用例结束后删除——共享测试库中有后续用例
# （如 m5）依赖「某领域/轨迹此前无其他活跃题」保证随机发题命中确定性。
_CREATED_QIDS: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup_created_questions(client, admin_headers):
    yield
    for qid in _CREATED_QIDS:
        client.delete(f"/api/questions/{qid}", headers=admin_headers)
    _CREATED_QIDS.clear()


def _app_payload(student_id, name, exam_mode="standard",
                 domains=("llm_arch", "memory", "agent")):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "female",
        "class_name": "计科244",
        "qq": "600600603",
        "lab_intent": False,
        "domains": list(domains),
        "focus_next_year": "research",
        "expectation": "开考二选一测试",
        "exam_mode": exam_mode,
    }


def _create_question(client, admin_headers, title, track="track1",
                     domain="llm_arch", active=True):
    res = client.post("/api/questions", json={
        "domain": domain, "track": track, "title": title,
        "body": f"{title} 的题面内容", "active": active,
    }, headers=admin_headers)
    assert res.status_code == 201, res.text
    qid = res.json()["id"]
    _CREATED_QIDS.append(qid)
    return qid


def _new_student(client, cid, student_id, name, exam_mode="standard"):
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload(student_id, name, exam_mode=exam_mode))
    assert res.status_code == 200, res.text
    return applicant_headers(client, cid, student_id)


def test_enroll_no_track_without_question_ids_400(client, cid):
    headers = _new_student(client, cid, "08265701", "缺方式")
    # track 缺省且无 question_ids → 400 未选择作答方式
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={}, headers=headers)
    assert res.status_code == 400
    assert "未选择作答方式" in str(res.json()["detail"])
    # track 空串且无 question_ids → 同样 400
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": ""}, headers=headers)
    assert res.status_code == 400
    assert "未选择作答方式" in str(res.json()["detail"])
    # track 空串 + 空 question_ids 列表 → 同样 400
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "", "question_ids": []}, headers=headers)
    assert res.status_code == 400
    assert "未选择作答方式" in str(res.json()["detail"])


def test_enroll_invalid_track_still_400(client, cid):
    headers = _new_student(client, cid, "08265702", "坏轨迹")
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track9"}, headers=headers)
    assert res.status_code == 400
    assert "无效轨迹" in str(res.json()["detail"])


def test_enroll_selected_track_happy(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "缺省自选甲", domain="llm_arch")
    q2 = _create_question(client, admin_headers, "缺省自选乙", domain="memory",
                          track="track2")
    headers = _new_student(client, cid, "08265703", "题库自选")

    # track 缺省 + question_ids → 成功且会话 track='selected'
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"question_ids": [q1, q2]}, headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["track"] == "selected"
    assert [q["id"] for q in data["questions"]] == [q1, q2]
    # domain_tags = 所选题目领域去重（而非全部报名领域）
    assert data["domain_tags"] == ["llm_arch", "memory"]


def test_enroll_selected_track_empty_string(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "空串自选", domain="agent")
    headers = _new_student(client, cid, "08265704", "空串轨迹")
    # track="" 与缺省同义
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "", "question_ids": [q1]}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["track"] == "selected"
    assert res.json()["domain_tags"] == ["agent"]


def test_enroll_selected_domain_tags_dedup(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "同域自选一", domain="llm_arch")
    q2 = _create_question(client, admin_headers, "同域自选二", domain="llm_arch",
                          track="track2")
    headers = _new_student(client, cid, "08265705", "同域去重")
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"question_ids": [q1, q2]}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["track"] == "selected"
    assert res.json()["domain_tags"] == ["llm_arch"]


def test_enroll_selected_outside_domain_403(client, cid, admin_headers):
    q_bad = _create_question(client, admin_headers, "缺省域外题", domain="infra")
    headers = _new_student(client, cid, "08265706", "缺省域外")
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"question_ids": [q_bad]}, headers=headers)
    assert res.status_code == 403
    assert "只能选问卷里报过的领域" in str(res.json()["detail"])


def test_selected_session_reroll_400(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "自选换题甲", domain="memory")
    headers = _new_student(client, cid, "08265707", "自选换题")
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"question_ids": [q1]}, headers=headers)
    assert res.status_code == 200, res.text
    sid, token = res.json()["session_id"], res.json()["session_token"]
    res = client.post(f"/api/campaigns/{cid}/exam/reroll",
                      json={"session_id": sid},
                      headers={**headers, "X-Session-Token": token})
    assert res.status_code == 400


def test_project_submit_with_zip_attachment(client, cid):
    headers = _new_student(client, cid, "08265708", "项目附件",
                           exam_mode="own_project")
    att_bytes = b"PK\x03\x04 project-zip-bytes"
    res = client.post(
        f"/api/campaigns/{cid}/applications/me/project",
        data={"repo_url": "https://github.com/alethic/with-att"},
        files={"attachment": ("project.zip", att_bytes, "application/zip")},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    session = res.json()
    assert session["track"] == "project"
    assert session["status"] == "uploaded"
    att_path = session["attachment_file_path"]
    assert att_path
    path = Path(att_path)
    assert path.is_file()
    assert path.name == "attachment.zip"
    assert path.parent.name == session["id"]
    assert path.read_bytes() == att_bytes


def test_project_submit_attachment_not_zip_400(client, cid):
    headers = _new_student(client, cid, "08265709", "非zip附件")
    res = client.post(
        f"/api/campaigns/{cid}/applications/me/project",
        data={"repo_url": "https://github.com/alethic/bad-att"},
        files={"attachment": ("notes.txt", b"not a zip", "text/plain")},
        headers=headers,
    )
    assert res.status_code == 400
    assert "zip" in str(res.json()["detail"])


def test_project_submit_attachment_too_large_413(client, cid):
    headers = _new_student(client, cid, "08265710", "超大附件")
    big = b"\0" * (50 * 1024 * 1024 + 1)
    res = client.post(
        f"/api/campaigns/{cid}/applications/me/project",
        data={"repo_url": "https://github.com/alethic/big-att"},
        files={"attachment": ("big.zip", big, "application/zip")},
        headers=headers,
    )
    assert res.status_code == 413
    # 超限不产生会话，可重新合法提交
    res = client.post(
        f"/api/campaigns/{cid}/applications/me/project",
        data={"repo_url": "https://github.com/alethic/big-att"},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["attachment_file_path"] is None
