"""按领域多次独立交卷（M12 增量）：track3 以领域为粒度去重——同领域重复交 409
（domain_already_uploaded），跨领域可继续交；track1/track2 一锤定音不变；
own_project 直提保持单个；GET exam/my_sessions 考生自查（只回本人、权限边界）。
"""
import sys
from pathlib import Path

from conftest import applicant_headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _app_payload(student_id, name, exam_mode="free_creation",
                 domains=("llm_arch", "memory", "agent")):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "female",
        "class_name": "计科244",
        "qq": "600600602",
        "lab_intent": False,
        "domains": list(domains),
        "focus_next_year": "research",
        "expectation": "按领域交卷",
        "exam_mode": exam_mode,
    }


def _create_question(client, admin_headers, title, track="track3",
                     domain="llm_arch"):
    res = client.post("/api/questions", json={
        "domain": domain, "track": track, "title": title,
        "body": f"{title} 的题面内容", "active": True,
    }, headers=admin_headers)
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _enroll(client, cid, headers, body):
    return client.post(f"/api/campaigns/{cid}/exam/enroll",
                       json=body, headers=headers)


def _upload(client, cid, session_id, token, repo_url=""):
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": session_id, "repo_url": repo_url},
        files={"cleaned": ("cleaned.jsonl",
                           b'{"event": "kimi_code_session", "turns": []}\n',
                           "application/octet-stream")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text
    return res.json()


def _new_student(client, cid, student_id, name, exam_mode="free_creation"):
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload(student_id, name, exam_mode=exam_mode))
    assert res.status_code == 200, res.text
    return applicant_headers(client, cid, student_id)


def test_track3_same_domain_resubmit_409(client, cid, admin_headers):
    headers = _new_student(client, cid, "08265101", "域重复")
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "llm_arch"})
    assert res.status_code == 200, res.text
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"],
            repo_url="https://github.com/a/llm")

    res = _enroll(client, cid, headers, {"track": "track3", "domain": "llm_arch"})
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "domain_already_uploaded"
    assert "llm_arch" in detail["domains"]
    assert "已交卷" in detail["message"]


def test_track3_other_domain_after_upload_200(client, cid, admin_headers):
    headers = _new_student(client, cid, "08265102", "跨域续交")
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "llm_arch"})
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"])

    res = _enroll(client, cid, headers, {"track": "track3", "domain": "memory"})
    assert res.status_code == 200, res.text
    assert res.json()["domain_tags"] == ["memory"]
    assert res.json()["questions"] == []
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"],
            repo_url="https://github.com/a/memory")

    res = _enroll(client, cid, headers, {"track": "track3", "domain": "agent"})
    assert res.status_code == 200, res.text
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"])
    # 三个领域都交完后，再交任何一个都是 409
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "llm_arch"})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "domain_already_uploaded"


def test_track3_question_ids_domain_scoped_dedup(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "按域题甲一", domain="llm_arch")
    q2 = _create_question(client, admin_headers, "按域题甲二", domain="llm_arch")
    q3 = _create_question(client, admin_headers, "按域题乙", domain="memory")
    headers = _new_student(client, cid, "08265103", "按域选题")

    res = _enroll(client, cid, headers,
                  {"track": "track3", "question_ids": [q1]})
    assert res.status_code == 200, res.text
    # 按领域交卷：选题开考的会话只记本次题目覆盖的领域（而非全部报名领域）
    assert res.json()["domain_tags"] == ["llm_arch"]
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"])

    # 同领域换一道题再交 → 409
    res = _enroll(client, cid, headers,
                  {"track": "track3", "question_ids": [q2]})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "domain_already_uploaded"
    assert res.json()["detail"]["domains"] == ["llm_arch"]
    # 换一个未交领域 → 200
    res = _enroll(client, cid, headers,
                  {"track": "track3", "question_ids": [q3]})
    assert res.status_code == 200, res.text
    assert res.json()["domain_tags"] == ["memory"]


def test_track3_multi_domain_session_covers_all_picked(client, cid,
                                                       admin_headers):
    q1 = _create_question(client, admin_headers, "多域题甲", domain="llm_arch")
    q2 = _create_question(client, admin_headers, "多域题乙", domain="memory")
    headers = _new_student(client, cid, "08265104", "多域一次")

    res = _enroll(client, cid, headers,
                  {"track": "track3", "question_ids": [q1, q2]})
    assert res.status_code == 200, res.text
    assert res.json()["domain_tags"] == ["llm_arch", "memory"]
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"])

    # 一次多域会话把两个领域都记为已交
    for d in ("llm_arch", "memory"):
        res = _enroll(client, cid, headers, {"track": "track3", "domain": d})
        assert res.status_code == 409, d
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "agent"})
    assert res.status_code == 200, res.text


def test_track12_uploaded_still_one_shot(client, cid, admin_headers):
    headers = _new_student(client, cid, "08265105", "一锤定音",
                           exam_mode="standard")
    res = _enroll(client, cid, headers, {"track": "track1"})
    assert res.status_code == 200, res.text
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"])

    # track1/track2 保持一锤定音：有 uploaded 会话即 409 already_uploaded
    for track in ("track1", "track2"):
        res = _enroll(client, cid, headers, {"track": track})
        assert res.status_code == 409, track
        assert res.json()["detail"]["code"] == "already_uploaded"
    # track3 与已交的全领域会话求交 → 同样被拒（domain 口径）
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "memory"})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "domain_already_uploaded"


def test_track3_draft_conflict_still_409(client, cid, admin_headers):
    headers = _new_student(client, cid, "08265106", "草稿冲突")
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "llm_arch"})
    assert res.status_code == 200, res.text
    # 未交卷的 draft 会话仍然阻塞新一轮 enroll（跨领域也一样）
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "memory"})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "already_enrolled"


def test_voided_domain_frees_resubmit(client, cid, admin_headers):
    headers = _new_student(client, cid, "08265107", "作废重交")
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "llm_arch"})
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"])
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "llm_arch"})
    assert res.status_code == 409

    res = client.post(f"/api/campaigns/{cid}/exam/void",
                      json={"student_id": "08265107"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    # 作废后该领域重新可交
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "llm_arch"})
    assert res.status_code == 200, res.text


def test_my_sessions_only_own_with_fields(client, cid, admin_headers):
    ha = _new_student(client, cid, "08265108", "自查甲")
    res = _enroll(client, cid, ha, {"track": "track3", "domain": "llm_arch"})
    sid_a = res.json()["session_id"]
    _upload(client, cid, sid_a, res.json()["session_token"],
            repo_url="https://github.com/a/llm")
    hb = _new_student(client, cid, "08265109", "自查乙")
    res = _enroll(client, cid, hb, {"track": "track3", "domain": "memory"})
    sid_b = res.json()["session_id"]

    res = client.get(f"/api/campaigns/{cid}/exam/my_sessions", headers=ha)
    assert res.status_code == 200, res.text
    mine = res.json()
    assert [s["session_id"] for s in mine] == [sid_a]
    s = mine[0]
    assert s["track"] == "track3"
    assert s["domain_tags"] == ["llm_arch"]
    assert s["status"] == "uploaded"
    assert s["uploaded_at"]
    assert s["repo_url"] == "https://github.com/a/llm"

    res = client.get(f"/api/campaigns/{cid}/exam/my_sessions", headers=hb)
    assert [s["session_id"] for s in res.json()] == [sid_b]
    sb = res.json()[0]
    assert sb["status"] == "draft"
    assert sb["uploaded_at"] is None


def test_my_sessions_marks_voided(client, cid, admin_headers):
    headers = _new_student(client, cid, "08265110", "作废标注")
    res = _enroll(client, cid, headers, {"track": "track3", "domain": "agent"})
    _upload(client, cid, res.json()["session_id"], res.json()["session_token"])
    res = client.post(f"/api/campaigns/{cid}/exam/void",
                      json={"student_id": "08265110"}, headers=admin_headers)
    assert res.status_code == 200

    mine = client.get(f"/api/campaigns/{cid}/exam/my_sessions",
                      headers=headers).json()
    assert len(mine) == 1
    assert mine[0]["status"] == "voided"  # 已作废的保留并标注，不计入已提交


def test_my_sessions_requires_applicant_auth(client, cid, admin_headers):
    res = client.get(f"/api/campaigns/{cid}/exam/my_sessions")
    assert res.status_code == 401
    # 管理员令牌也不是考生会话 → 401
    res = client.get(f"/api/campaigns/{cid}/exam/my_sessions",
                     headers=admin_headers)
    assert res.status_code == 401


def test_my_sessions_cross_campaign_403(client, cid, admin_headers):
    headers = _new_student(client, cid, "08265111", "跨届次")
    # 登录会话绑定届次：拿本届 token 访问其它届次 → 403（require_applicant 先行）
    res = client.get("/api/campaigns/2099/exam/my_sessions", headers=headers)
    assert res.status_code == 403
