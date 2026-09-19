"""M11 自由创作型自选领域约束：题目领域必须 ⊆ 考生报名领域、一个领域一道题、1~3 题。

考生报名领域夹具为 ["llm_arch", "memory", "agent"]（最多 3 个）。
"""
import sys

from conftest import applicant_headers

sys.path.insert(0, ".")


def _app_payload(student_id, name, exam_mode="standard",
                 domains=("llm_arch", "memory", "agent")):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "female",
        "class_name": "计科244",
        "qq": "600600601",
        "lab_intent": False,
        "domains": list(domains),
        "focus_next_year": "research",
        "expectation": "自由创作自选领域约束",
        "exam_mode": exam_mode,
    }


def _create_question(client, admin_headers, title, track="track1",
                     domain="llm_arch", active=True):
    res = client.post("/api/questions", json={
        "domain": domain, "track": track, "title": title,
        "body": f"{title} 的题面内容", "active": active,
    }, headers=admin_headers)
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_track3_same_domain_two_questions_rejected(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "自由同域一", track="track3",
                          domain="llm_arch")
    q2 = _create_question(client, admin_headers, "自由同域二", track="track3",
                          domain="llm_arch")
    student = "08265011"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "自由同域"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3", "question_ids": [q1, q2]},
                      headers=headers)
    assert res.status_code == 400
    assert "同一领域只能选择一道题作答" in str(res.json()["detail"])


def test_track3_multi_domain_ok(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "自由域甲", track="track3",
                          domain="llm_arch")
    q2 = _create_question(client, admin_headers, "自由域乙", track="track3",
                          domain="memory")
    q3 = _create_question(client, admin_headers, "自由域丙", track="track2",
                          domain="agent")
    student = "08265012"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "自由多域", exam_mode="free_creation"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3", "question_ids": [q1, q2, q3]},
                      headers=headers)
    assert res.status_code == 200, res.text
    served = res.json()["questions"]
    assert res.json()["track"] == "track3"
    # 按提交顺序保序下发
    assert [q["id"] for q in served] == [q1, q2, q3]
    # 每个领域恰好一题
    assert len({q["domain"] for q in served}) == len(served) == 3


def test_track3_two_domains_ok(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "自由双域一", track="track1",
                          domain="llm_arch")
    q2 = _create_question(client, admin_headers, "自由双域二", track="track3",
                          domain="memory")
    student = "08265013"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "自由双域", exam_mode="free_creation"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3", "question_ids": [q1, q2]},
                      headers=headers)
    assert res.status_code == 200, res.text
    assert [q["id"] for q in res.json()["questions"]] == [q1, q2]


def test_track3_outside_domain_rejected(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "自由域外", track="track1",
                          domain="infra")  # 不在考生领域内
    student = "08265014"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "自由域外"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3", "question_ids": [q1]},
                      headers=headers)
    assert res.status_code == 400
    assert "只能选择自己报名领域内的题目" in str(res.json()["detail"])


def test_track3_mixed_outside_and_inside_rejected(client, cid, admin_headers):
    q_ok = _create_question(client, admin_headers, "自由域内混选", track="track3",
                            domain="memory")
    q_bad = _create_question(client, admin_headers, "自由域外混选", track="track3",
                             domain="cv_arch")
    student = "08265015"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "自由混选"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3", "question_ids": [q_ok, q_bad]},
                      headers=headers)
    assert res.status_code == 400
    assert "只能选择自己报名领域内的题目" in str(res.json()["detail"])


def test_track3_single_ok(client, cid, admin_headers):
    q1 = _create_question(client, admin_headers, "自由单题", track="track3",
                          domain="llm_arch")
    student = "08265016"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "自由单题", exam_mode="free_creation"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3", "question_ids": [q1]},
                      headers=headers)
    assert res.status_code == 200, res.text
    assert [q["id"] for q in res.json()["questions"]] == [q1]


def test_track3_invalid_question_still_400(client, cid, admin_headers):
    # 存在性与 active 校验先于领域约束（顺序：先查存在/active，再查领域归属，再查重复）
    student = "08265017"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "自由无效"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3", "question_ids": ["nonexistent0001"]},
                      headers=headers)
    assert res.status_code == 400
    assert "题目不存在或未开放" in str(res.json()["detail"])

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3", "question_ids": []},
                      headers=headers)
    assert res.status_code == 400
    assert "自选题目数须为 1-3 道" in str(res.json()["detail"])


def test_track12_selfselect_unchanged(client, cid, admin_headers):
    # track1/track2 保持 1~3 题不变，不做领域去重/归属校验（同域两题仍可）
    q1 = _create_question(client, admin_headers, "常规自选一", track="track2",
                          domain="llm_arch")
    q2 = _create_question(client, admin_headers, "常规自选二", track="track2",
                          domain="llm_arch")
    student = "08265018"
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload(student, "常规两题"))
    headers = applicant_headers(client, cid, student)

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track2", "question_ids": [q1, q2]},
                      headers=headers)
    assert res.status_code == 200, res.text
    assert [q["id"] for q in res.json()["questions"]] == [q1, q2]
