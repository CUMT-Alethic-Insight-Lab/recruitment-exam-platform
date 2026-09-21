import sys
from pathlib import Path

from app import db
from conftest import applicant_headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _app_payload(student_id="08266001", name="考拉一号", domains=None):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "male",
        "class_name": "计科244",
        "qq": "600600600",
        "lab_intent": True,
        "domains": domains or ["llm_arch", "ai_safety"],
        "focus_next_year": "project",
        "expectation": "测试报名",
    }


def _question_payload(domain, track, title):
    return {"domain": domain, "track": track, "title": title,
            "body": f"{title}：请论述……", "tags": []}


def test_question_crud(client, admin_headers):
    res = client.post("/api/questions", json=_question_payload("llm_arch", "track1", "q-t1-1"),
                      headers=admin_headers)
    assert res.status_code == 201
    qid = res.json()["id"]

    bad = client.post("/api/questions", json=_question_payload("nope", "track1", "bad"),
                      headers=admin_headers)
    assert bad.status_code == 400
    bad2 = client.post("/api/questions", json=_question_payload("llm_arch", "track9", "bad"),
                       headers=admin_headers)
    assert bad2.status_code == 400

    res = client.get("/api/questions?domain=llm_arch&track=track1", headers=admin_headers)
    assert res.status_code == 200
    assert any(q["id"] == qid for q in res.json())

    res = client.patch(f"/api/questions/{qid}", json={"title": "q-t1-1-改"}, headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["title"] == "q-t1-1-改"

    res = client.delete(f"/api/questions/{qid}", headers=admin_headers)
    assert res.status_code == 200


def test_questions_require_admin(client):
    assert client.get("/api/questions").status_code == 401


def test_enroll_no_application(client, cid):
    student = "08269990"
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student))
    assert res.status_code == 200
    headers = applicant_headers(client, cid, student)
    with db.connection() as conn:
        conn.execute("DELETE FROM application WHERE campaign_id = ? AND student_id = ?",
                     (cid, student))
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "no_application"


def test_enroll_requires_login_401(client, cid):
    res = client.post(f"/api/campaigns/{cid}/exam/enroll", json={"track": "track1"})
    assert res.status_code == 401


def test_enroll_track1_serves_questions(client, cid, admin_headers):
    for i, dom in enumerate(["llm_arch", "ai_safety"]):
        for j in range(2):
            p = _question_payload(dom, "track1", f"T1-{dom}-{j}")
            client.post("/api/questions", json=p, headers=admin_headers)
        for j in range(2):
            p = _question_payload(dom, "track2", f"T2-{dom}-{j}")
            client.post("/api/questions", json=p, headers=admin_headers)

    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload("08266001", domains=["llm_arch", "ai_safety"]))
    assert res.status_code == 200
    headers = applicant_headers(client, cid, "08266001")

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["track"] == "track1"
    assert len(data["questions"]) == 4
    assert data["session_token"]
    assert data["machine_id"]
    assert set(d["domain"] for d in data["questions"]) == {"llm_arch", "ai_safety"}


def test_enroll_track3_without_domain_400(client, cid):
    # M12：track3 不再随机发题——无 question_ids 且未给 domain 一律 400
    client.post(f"/api/campaigns/{cid}/applications",
                json=_app_payload("08266003", domains=["memory"]))
    headers = applicant_headers(client, cid, "08266003")
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track3"}, headers=headers)
    assert res.status_code == 400
    assert "请先选择领域" in str(res.json()["detail"])


def test_reroll(client, cid, admin_headers):
    student = "08266005"
    for dom in ["llm_arch", "ai_safety"]:
        for j in range(2):
            client.post("/api/questions", json=_question_payload(dom, "track1", f"T1-{dom}-{j}"),
                        headers=admin_headers)
    client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student))
    headers = applicant_headers(client, cid, student)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200
    sid = res.json()["session_id"]
    token = res.json()["session_token"]

    res = client.post(f"/api/campaigns/{cid}/exam/reroll",
                      json={"session_id": sid},
                      headers={**headers, "X-Session-Token": "wrong"})
    assert res.status_code == 401

    res = client.post(f"/api/campaigns/{cid}/exam/reroll",
                      json={"session_id": sid},
                      headers={**headers, "X-Session-Token": token})
    assert res.status_code == 200
    assert res.json()["rerolls_left"] == 1

    res = client.post(f"/api/campaigns/{cid}/exam/reroll",
                      json={"session_id": sid},
                      headers={**headers, "X-Session-Token": token})
    assert res.status_code == 200
    assert res.json()["rerolls_left"] == 0

    res = client.post(f"/api/campaigns/{cid}/exam/reroll",
                      json={"session_id": sid},
                      headers={**headers, "X-Session-Token": token})
    assert res.status_code == 409


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_sessions_list_and_void(client, cid, admin_headers):
    res = client.get(f"/api/campaigns/{cid}/exam/sessions", headers=admin_headers)
    assert res.status_code == 200
    assert any(s["student_id"] == "08266001" for s in res.json())

    res = client.post(f"/api/campaigns/{cid}/exam/void",
                      json={"student_id": "08266001"}, headers=admin_headers)
    assert res.status_code == 200

    headers = applicant_headers(client, cid, "08266001")
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track2"}, headers=headers)
    assert res.status_code == 200
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track2"}, headers=headers)
    assert res.status_code == 409


def test_void_uploaded_allows_retake(client, cid, admin_headers):
    sid = "08266002"
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload(sid, name="重考二号"))
    assert res.status_code == 200
    headers = applicant_headers(client, cid, sid)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200
    session_id, token = res.json()["session_id"], res.json()["session_token"]

    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": session_id},
        files={"cleaned": ("cleaned.jsonl",
                           b'{"event": "kimi_code_session", "turns": [1]}\n',
                           "application/octet-stream")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "uploaded"

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "already_uploaded"

    res = client.post(f"/api/campaigns/{cid}/exam/void",
                      json={"student_id": sid}, headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["status"] == "voided"

    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200


def test_void_without_session_404(client, cid, admin_headers):
    res = client.post(f"/api/campaigns/{cid}/exam/void",
                      json={"student_id": "08269999"}, headers=admin_headers)
    assert res.status_code == 404
