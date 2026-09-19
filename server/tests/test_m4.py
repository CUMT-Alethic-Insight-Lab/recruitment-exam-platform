from app import db
from conftest import applicant_headers

CLEANED_CONTENT = '{"event": "kimi_code_session", "turns": [1, 2, 3]}\n'


def _app_payload(student_id):
    return {
        "student_id": student_id,
        "name": f"评审测试{student_id[-2:]}",
        "gender": "male",
        "class_name": "计科244",
        "qq": "600600600",
        "lab_intent": True,
        "domains": ["llm_arch", "ai_safety"],
        "focus_next_year": "project",
        "expectation": "M4 评审测试",
    }


def _enroll_and_upload(client, cid, student_id):
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student_id))
    assert res.status_code == 200
    headers = applicant_headers(client, cid, student_id)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200
    data = res.json()
    sid, token = data["session_id"], data["session_token"]
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": sid},
        files={"cleaned": ("cleaned.jsonl", CLEANED_CONTENT.encode("utf-8"),
                           "application/octet-stream")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text
    return sid


def _enroll_draft(client, cid, student_id):
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student_id))
    assert res.status_code == 200
    headers = applicant_headers(client, cid, student_id)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200
    return res.json()["session_id"]


def test_score_success_and_fields(client, cid, admin_headers):
    sid = _enroll_and_upload(client, cid, "08268001")
    res = client.post(
        f"/api/campaigns/{cid}/sessions/{sid}/score",
        json={"increment_score": 88.5, "human_rate": 90, "comment": "表现不错",
              "scorer": "评委A"},
        headers=admin_headers,
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["session_id"] == sid
    assert out["increment_score"] == 88.5
    assert out["human_rate"] == 90
    assert out["comment"] == "表现不错"
    assert out["scorer"] == "评委A"
    assert out["created_at"] and out["updated_at"]


def test_score_overwrite_keeps_created_at(client, cid, admin_headers):
    sid = _enroll_and_upload(client, cid, "08268002")
    res = client.post(
        f"/api/campaigns/{cid}/sessions/{sid}/score",
        json={"increment_score": 70, "human_rate": 75},
        headers=admin_headers,
    )
    assert res.status_code == 200
    first = res.json()
    res = client.post(
        f"/api/campaigns/{cid}/sessions/{sid}/score",
        json={"increment_score": 95, "human_rate": 92, "comment": "覆写",
              "scorer": "评委B"},
        headers=admin_headers,
    )
    assert res.status_code == 200
    second = res.json()
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]
    assert second["increment_score"] == 95
    assert second["scorer"] == "评委B"


def test_score_out_of_range_400(client, cid, admin_headers):
    sid = _enroll_and_upload(client, cid, "08268003")
    res = client.post(
        f"/api/campaigns/{cid}/sessions/{sid}/score",
        json={"increment_score": 150, "human_rate": 80},
        headers=admin_headers,
    )
    assert res.status_code == 400
    res = client.post(
        f"/api/campaigns/{cid}/sessions/{sid}/score",
        json={"increment_score": 50, "human_rate": -1},
        headers=admin_headers,
    )
    assert res.status_code == 400


def test_score_draft_session_409(client, cid, admin_headers):
    sid = _enroll_draft(client, cid, "08268004")
    res = client.post(
        f"/api/campaigns/{cid}/sessions/{sid}/score",
        json={"increment_score": 60, "human_rate": 60},
        headers=admin_headers,
    )
    assert res.status_code == 409


def test_review_sessions_list(client, cid, admin_headers):
    sid = _enroll_and_upload(client, cid, "08268005")
    res = client.post(
        f"/api/campaigns/{cid}/sessions/{sid}/score",
        json={"increment_score": 77, "human_rate": 81, "scorer": "评委C"},
        headers=admin_headers,
    )
    assert res.status_code == 200

    res = client.get(f"/api/campaigns/{cid}/review/sessions", headers=admin_headers)
    assert res.status_code == 200, res.text
    sessions = res.json()
    assert isinstance(sessions, list)
    target = [s for s in sessions if s["session_id"] == sid]
    assert len(target) == 1
    item = target[0]
    assert item["student_name"] == "评审测试05"
    assert item["increment_score"] == 77
    assert item["human_rate"] == 81
    assert item["scorer"] == "评委C"
    assert item["score_updated_at"]

    # 未打分但已交卷的会话分数字段为 null
    sid2 = _enroll_and_upload(client, cid, "08268006")
    item2 = [s for s in res.json() if s["session_id"] == sid2]
    # 上面拿的是打分前的列表，重新拉一次
    res = client.get(f"/api/campaigns/{cid}/review/sessions", headers=admin_headers)
    item2 = [s for s in res.json() if s["session_id"] == sid2]
    assert len(item2) == 1
    assert item2[0]["student_name"] == "评审测试06"
    assert item2[0]["increment_score"] is None
    assert item2[0]["human_rate"] is None

    # 按 uploaded_at 倒序
    uploaded_ats = [s["uploaded_at"] for s in res.json()]
    assert uploaded_ats == sorted(uploaded_ats, reverse=True)


def test_cleaned_endpoint_returns_array(client, cid, admin_headers):
    sid = _enroll_and_upload(client, cid, "08268007")
    res = client.get(f"/api/campaigns/{cid}/sessions/{sid}/cleaned",
                     headers=admin_headers)
    assert res.status_code == 200, res.text
    items = res.json()
    assert isinstance(items, list)
    assert len(items) == 1
    assert items[0]["event"] == "kimi_code_session"

    # 追加一行损坏内容后仍 200，坏行被跳过
    with db.connection() as conn:
        row = conn.execute("SELECT cleaned_file_path FROM exam_session WHERE id = ?",
                           (sid,)).fetchone()
    path = row["cleaned_file_path"]
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"ok": true, "n": 2}\n{broken json\n')
    res = client.get(f"/api/campaigns/{cid}/sessions/{sid}/cleaned",
                     headers=admin_headers)
    assert res.status_code == 200
    items = res.json()
    assert isinstance(items, list)
    assert len(items) == 2
    assert items[1] == {"ok": True, "n": 2}


def test_cleaned_missing_404(client, cid, admin_headers):
    # draft 会话没有 cleaned 文件
    sid = _enroll_draft(client, cid, "08268008")
    res = client.get(f"/api/campaigns/{cid}/sessions/{sid}/cleaned",
                     headers=admin_headers)
    assert res.status_code == 404
    # 会话不存在
    res = client.get(f"/api/campaigns/{cid}/sessions/no-such-sid/cleaned",
                     headers=admin_headers)
    assert res.status_code == 404


def test_review_requires_admin_401(client, cid):
    res = client.get(f"/api/campaigns/{cid}/review/sessions")
    assert res.status_code == 401
    res = client.post(f"/api/campaigns/{cid}/sessions/some-sid/score",
                      json={"increment_score": 50, "human_rate": 50})
    assert res.status_code == 401
    res = client.get(f"/api/campaigns/{cid}/sessions/some-sid/cleaned")
    assert res.status_code == 401
