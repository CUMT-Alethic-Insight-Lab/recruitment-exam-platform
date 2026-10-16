import uuid
from datetime import date, timedelta

from app import db
from conftest import applicant_headers

CLEANED_CONTENT = '{"event": "kimi_code_session", "turns": [1]}\n'

# 每个用例用独立日期，避免共享测试库里的时段互相干扰
D_GEN = (date.today() + timedelta(days=40)).isoformat()
D_BOOK = (date.today() + timedelta(days=41)).isoformat()
D_RACE = (date.today() + timedelta(days=42)).isoformat()
D_CANCEL = (date.today() + timedelta(days=43)).isoformat()
D_ADMIN = (date.today() + timedelta(days=44)).isoformat()
D_BRIEF = (date.today() + timedelta(days=45)).isoformat()
D_MULTI = (date.today() + timedelta(days=46)).isoformat()
D_MODE = (date.today() + timedelta(days=47)).isoformat()
D_MODE2 = (date.today() + timedelta(days=48)).isoformat()


def _app_payload(student_id):
    return {
        "student_id": student_id,
        "name": f"M23考生{student_id[-2:]}",
        "gender": "male",
        "class_name": "计科244",
        "qq": "600600600",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "M23 面试时段预约测试",
    }


def _apply(client, cid, admin_headers, student_id, status=None):
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload(student_id))
    assert res.status_code == 200, res.text
    if status is not None:
        res = client.patch(f"/api/campaigns/{cid}/applications/{student_id}/status",
                           json={"status": status}, headers=admin_headers)
        assert res.status_code == 200, res.text


def _generate(client, cid, admin_headers, day, start="10:00", end="12:00",
              slot_minutes=45, note=""):
    res = client.post(
        f"/api/campaigns/{cid}/interview-slots/generate",
        json={"date": day, "windows": [{"start": start, "end": end}],
              "slot_minutes": slot_minutes, "note": note},
        headers=admin_headers)
    assert res.status_code == 200, res.text
    return res.json()


def _enroll_and_upload(client, cid, student_id, track="track1"):
    headers = applicant_headers(client, cid, student_id)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": track}, headers=headers)
    assert res.status_code == 200, res.text
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


def _insert_past_slot(cid, start_at, end_at, booked_by=None, booked_at=None):
    slot_id = uuid.uuid4().hex[:16]
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO interview_slot"
            " (id, campaign_id, start_at, end_at, note, booked_by, booked_at, created_at)"
            " VALUES (?, ?, ?, ?, '', ?, ?, ?)",
            (slot_id, cid, start_at, end_at, booked_by, booked_at,
             "2026-01-01T00:00:00+00:00"),
        )
    return slot_id


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def test_generate_counts_and_skip_duplicates(client, cid, admin_headers):
    # 10:00-12:00，45 分钟一格 → 10:00 / 10:45 两格（11:30+45 超出窗口）
    out = _generate(client, cid, admin_headers, D_GEN, note="上午场")
    assert out["created"] == 2
    assert out["skipped"] == 0
    slots = out["slots"]
    assert len(slots) == 2
    assert slots[0]["start_at"] == f"{D_GEN}T10:00:00+08:00"
    assert slots[0]["end_at"] == f"{D_GEN}T10:45:00+08:00"
    assert slots[-1]["start_at"] == f"{D_GEN}T10:45:00+08:00"
    assert slots[-1]["end_at"] == f"{D_GEN}T11:30:00+08:00"
    for s in slots:
        assert s["note"] == "上午场"
        assert s["booked_by"] is None
        assert s["booked_at"] is None
        assert s["student_name"] is None

    # 重复生成：同 start_at 全部跳过
    out = _generate(client, cid, admin_headers, D_GEN)
    assert out["created"] == 0
    assert out["skipped"] == 2

    # 管理端列表按 start_at 升序且包含这些槽
    res = client.get(f"/api/campaigns/{cid}/interview-slots", headers=admin_headers)
    assert res.status_code == 200, res.text
    starts = [s["start_at"] for s in res.json()]
    assert starts == sorted(starts)
    assert f"{D_GEN}T10:00:00+08:00" in starts


def test_generate_invalid_400(client, cid, admin_headers):
    base = {"date": D_GEN, "windows": [{"start": "10:00", "end": "12:00"}],
            "slot_minutes": 45, "note": ""}
    # slot_minutes 越界
    for bad in (10, 121):
        res = client.post(f"/api/campaigns/{cid}/interview-slots/generate",
                          json={**base, "slot_minutes": bad}, headers=admin_headers)
        assert res.status_code == 400, res.text
    # 非法 HH:MM
    res = client.post(f"/api/campaigns/{cid}/interview-slots/generate",
                      json={**base, "windows": [{"start": "25:00", "end": "12:00"}]},
                      headers=admin_headers)
    assert res.status_code == 400
    # start >= end
    res = client.post(f"/api/campaigns/{cid}/interview-slots/generate",
                      json={**base, "windows": [{"start": "12:00", "end": "10:00"}]},
                      headers=admin_headers)
    assert res.status_code == 400
    # 非法日期
    res = client.post(f"/api/campaigns/{cid}/interview-slots/generate",
                      json={**base, "date": "2026-13-40"}, headers=admin_headers)
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# available / book
# ---------------------------------------------------------------------------

def test_available_forbidden_for_non_shortlisted(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269301")  # applied
    headers = applicant_headers(client, cid, "08269301")
    res = client.get(f"/api/campaigns/{cid}/interview-slots/available", headers=headers)
    assert res.status_code == 403
    assert res.json()["detail"] == "当前状态不可预约面试"


def test_book_success_and_available_view(client, cid, admin_headers):
    out = _generate(client, cid, admin_headers, D_BOOK)
    slot = out["slots"][0]
    _apply(client, cid, admin_headers, "08269302", status="shortlisted")
    _apply(client, cid, admin_headers, "08269303", status="shortlisted")
    headers = applicant_headers(client, cid, "08269302")

    res = client.get(f"/api/campaigns/{cid}/interview-slots/available", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["eligible"] is True
    assert data["my_booking"] is None
    # 只含未来时段，taken 不暴露身份
    day_slots = [s for s in data["slots"] if s["start_at"].startswith(D_BOOK)]
    assert len(day_slots) == 2
    assert all(set(s.keys()) == {"id", "start_at", "end_at", "note", "taken"}
               for s in day_slots)
    assert all(s["taken"] is False for s in day_slots)

    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"mode": "online"}, headers=headers)
    assert res.status_code == 200, res.text
    booking = res.json()
    assert booking["id"] == slot["id"]
    assert booking["start_at"] == slot["start_at"]
    assert booking["booked_at"]
    assert booking["booked_mode"] == "online"

    res = client.get(f"/api/campaigns/{cid}/interview-slots/available", headers=headers)
    data = res.json()
    assert data["my_booking"]["id"] == slot["id"]
    assert data["my_booking"]["booked_mode"] == "online"
    mine = [s for s in data["slots"] if s["id"] == slot["id"]][0]
    assert mine["taken"] is True

    # 第三人看到的 available 里该槽也是 taken，但不暴露学号
    other = applicant_headers(client, cid, "08269303")
    res = client.get(f"/api/campaigns/{cid}/interview-slots/available", headers=other)
    assert "08269302" not in res.text


def test_book_second_slot_conflict_409(client, cid, admin_headers):
    _generate(client, cid, admin_headers, D_MULTI)
    _apply(client, cid, admin_headers, "08269304", status="shortlisted")
    headers = applicant_headers(client, cid, "08269304")
    res = client.get(f"/api/campaigns/{cid}/interview-slots", headers=admin_headers)
    free = [s for s in res.json()
            if s["start_at"].startswith(D_MULTI) and s["booked_by"] is None]
    assert len(free) >= 2
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{free[0]['id']}/book",
                      json={"mode": "offline"}, headers=headers)
    assert res.status_code == 200, res.text
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{free[1]['id']}/book",
                      json={"mode": "offline"}, headers=headers)
    assert res.status_code == 409
    assert res.json()["detail"] == "你已有预约，请先取消再改约"


def test_two_applicants_race_loser_409(client, cid, admin_headers):
    out = _generate(client, cid, admin_headers, D_RACE)
    slot = out["slots"][0]
    _apply(client, cid, admin_headers, "08269305", status="shortlisted")
    _apply(client, cid, admin_headers, "08269306", status="shortlisted")
    h1 = applicant_headers(client, cid, "08269305")
    h2 = applicant_headers(client, cid, "08269306")
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"mode": "online"}, headers=h1)
    assert res.status_code == 200, res.text
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"mode": "offline"}, headers=h2)
    assert res.status_code == 409
    assert res.json()["detail"] == "该时段刚被抢走，请刷新重选"


def test_book_mode_required_and_validated(client, cid, admin_headers):
    out = _generate(client, cid, admin_headers, D_MODE)
    slot = out["slots"][0]
    _apply(client, cid, admin_headers, "08269315", status="shortlisted")
    headers = applicant_headers(client, cid, "08269315")
    # 缺 mode
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={}, headers=headers)
    assert res.status_code == 400
    assert res.json()["detail"] == "请选择线上或线下面试"
    # 非法 mode
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"mode": "remote"}, headers=headers)
    assert res.status_code == 400
    assert res.json()["detail"] == "请选择线上或线下面试"
    # 合法 mode 成功，AdminSlotOut 带 booked_mode
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"mode": "offline"}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["booked_mode"] == "offline"
    res = client.get(f"/api/campaigns/{cid}/interview-slots", headers=admin_headers)
    row = [s for s in res.json() if s["id"] == slot["id"]][0]
    assert row["booked_mode"] == "offline"


def test_online_offline_each_book_one(client, cid, admin_headers):
    out = _generate(client, cid, admin_headers, D_MODE2)
    s1, s2 = out["slots"][0], out["slots"][1]
    _apply(client, cid, admin_headers, "08269316", status="shortlisted")
    _apply(client, cid, admin_headers, "08269317", status="shortlisted")
    h1 = applicant_headers(client, cid, "08269316")
    h2 = applicant_headers(client, cid, "08269317")
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{s1['id']}/book",
                      json={"mode": "online"}, headers=h1)
    assert res.status_code == 200, res.text
    assert res.json()["booked_mode"] == "online"
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{s2['id']}/book",
                      json={"mode": "offline"}, headers=h2)
    assert res.status_code == 200, res.text
    assert res.json()["booked_mode"] == "offline"
    res = client.get(f"/api/campaigns/{cid}/interview-slots", headers=admin_headers)
    modes = {s["id"]: s["booked_mode"] for s in res.json()
             if s["id"] in (s1["id"], s2["id"])}
    assert modes == {s1["id"]: "online", s2["id"]: "offline"}


def test_book_non_shortlisted_403(client, cid, admin_headers):
    out = _generate(client, cid, admin_headers, D_CANCEL)
    slot = out["slots"][0]
    _apply(client, cid, admin_headers, "08269307", status="tested")
    headers = applicant_headers(client, cid, "08269307")
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"mode": "online"}, headers=headers)
    assert res.status_code == 403
    assert res.json()["detail"] == "当前状态不可预约面试"


def test_cancel_then_rebook(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269308", status="shortlisted")
    headers = applicant_headers(client, cid, "08269308")
    res = client.get(f"/api/campaigns/{cid}/interview-slots", headers=admin_headers)
    free = [s for s in res.json()
            if s["start_at"].startswith(D_CANCEL) and s["booked_by"] is None]
    assert len(free) >= 2

    # 无预订时取消 → 404
    res = client.post(f"/api/campaigns/{cid}/interview-slots/mine/cancel",
                      headers=headers)
    assert res.status_code == 404

    res = client.post(f"/api/campaigns/{cid}/interview-slots/{free[0]['id']}/book",
                      json={"mode": "online"}, headers=headers)
    assert res.status_code == 200, res.text
    res = client.post(f"/api/campaigns/{cid}/interview-slots/mine/cancel",
                      headers=headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}

    # 取消后槽恢复空闲（booked_mode 一并清空），可改约另一格
    res = client.get(f"/api/campaigns/{cid}/interview-slots/available", headers=headers)
    assert res.json()["my_booking"] is None
    res = client.get(f"/api/campaigns/{cid}/interview-slots", headers=admin_headers)
    row = [s for s in res.json() if s["id"] == free[0]["id"]][0]
    assert row["booked_mode"] is None
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{free[1]['id']}/book",
                      json={"mode": "offline"}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["booked_mode"] == "offline"


def test_past_slot_book_and_cancel_400(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269309", status="shortlisted")
    headers = applicant_headers(client, cid, "08269309")
    past = _insert_past_slot(cid, "2020-01-01T10:00:00+08:00",
                             "2020-01-01T10:45:00+08:00")
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{past}/book",
                      json={"mode": "online"}, headers=headers)
    assert res.status_code == 400

    # 已约的过去时段：cancel → 400
    _apply(client, cid, admin_headers, "08269310", status="shortlisted")
    past2 = _insert_past_slot(cid, "2020-01-02T10:00:00+08:00",
                              "2020-01-02T10:45:00+08:00",
                              booked_by="08269310",
                              booked_at="2020-01-01T00:00:00+00:00")
    h2 = applicant_headers(client, cid, "08269310")
    res = client.post(f"/api/campaigns/{cid}/interview-slots/mine/cancel", headers=h2)
    assert res.status_code == 400
    assert res.json()["detail"] == "时段已开始，无法取消，如需改约请联系管理员"
    # 已约过去时段的人（即使状态不再是 shortlisted）仍可查看自己的预约
    res = client.get(f"/api/campaigns/{cid}/interview-slots/available", headers=h2)
    assert res.status_code == 200, res.text
    assert res.json()["my_booking"]["id"] == past2
    # 过去时段不出现在可约列表
    assert all(s["id"] != past2 for s in res.json()["slots"])


# ---------------------------------------------------------------------------
# admin: delete / unbook
# ---------------------------------------------------------------------------

def test_delete_booked_409_unbook_then_rebook(client, cid, admin_headers):
    out = _generate(client, cid, admin_headers, D_ADMIN)
    slot = out["slots"][0]
    _apply(client, cid, admin_headers, "08269311", status="shortlisted")
    headers = applicant_headers(client, cid, "08269311")
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"mode": "offline"}, headers=headers)
    assert res.status_code == 200, res.text

    # 已约槽不可删
    res = client.delete(f"/api/campaigns/{cid}/interview-slots/{slot['id']}",
                        headers=admin_headers)
    assert res.status_code == 409
    assert res.json()["detail"] == "该时段已有预约，请先取消预约"

    # 管理端列表带姓名与预约方式
    res = client.get(f"/api/campaigns/{cid}/interview-slots", headers=admin_headers)
    row = [s for s in res.json() if s["id"] == slot["id"]][0]
    assert row["booked_by"] == "08269311"
    assert row["student_name"] == "M23考生11"
    assert row["booked_at"]
    assert row["booked_mode"] == "offline"

    # unbook 后清空（含 booked_mode），考生可再订
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/unbook",
                      headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["booked_by"] is None
    assert data["booked_at"] is None
    assert data["booked_mode"] is None
    assert data["student_name"] is None

    res = client.delete(f"/api/campaigns/{cid}/interview-slots/{slot['id']}",
                        headers=admin_headers)
    assert res.status_code == 200, res.text

    out = _generate(client, cid, admin_headers, D_ADMIN, start="14:00", end="15:00")
    slot2 = out["slots"][0]
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot2['id']}/book",
                      json={"mode": "online"}, headers=headers)
    assert res.status_code == 200, res.text

    # 不存在的槽
    res = client.delete(f"/api/campaigns/{cid}/interview-slots/nope",
                        headers=admin_headers)
    assert res.status_code == 404
    res = client.post(f"/api/campaigns/{cid}/interview-slots/nope/unbook",
                      headers=admin_headers)
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# PATCH comment：评语随时可改
# ---------------------------------------------------------------------------

def test_patch_comment_creates_row_then_preserves_scores(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269312", status="tested")
    sid = _enroll_and_upload(client, cid, "08269312")

    # 无评分行时先写评语 → 建行，分数为 NULL
    res = client.patch(f"/api/campaigns/{cid}/sessions/{sid}/comment",
                       json={"comment": "先占个评语"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["session_id"] == sid
    assert out["comment"] == "先占个评语"
    assert out["updated_at"]
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM exam_score WHERE session_id = ?",
                           (sid,)).fetchone()
    assert row["increment_score"] is None
    assert row["human_rate"] is None
    assert row["scorer"] == ""

    # 打分后再改评语：分数不动
    res = client.post(f"/api/campaigns/{cid}/sessions/{sid}/score",
                      json={"increment_score": 88, "human_rate": 90,
                            "comment": "打分时的评语", "scorer": "评委A"},
                      headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.patch(f"/api/campaigns/{cid}/sessions/{sid}/comment",
                       json={"comment": "事后补充的评语"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json()["comment"] == "事后补充的评语"
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM exam_score WHERE session_id = ?",
                           (sid,)).fetchone()
    assert row["increment_score"] == 88
    assert row["human_rate"] == 90
    assert row["scorer"] == "评委A"

    # 会话不存在 → 404
    res = client.patch(f"/api/campaigns/{cid}/sessions/nope/comment",
                       json={"comment": "x"}, headers=admin_headers)
    assert res.status_code == 404
    # 需要 admin
    res = client.patch(f"/api/campaigns/{cid}/sessions/{sid}/comment",
                       json={"comment": "x"})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# candidates / brief 带 booking / session_id
# ---------------------------------------------------------------------------

def test_candidates_and_brief_include_booking_and_session_id(client, cid,
                                                             admin_headers):
    out = _generate(client, cid, admin_headers, D_BRIEF)
    slot = out["slots"][0]
    _apply(client, cid, admin_headers, "08269313", status="shortlisted")
    sid = _enroll_and_upload(client, cid, "08269313")
    headers = applicant_headers(client, cid, "08269313")
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"mode": "online"}, headers=headers)
    assert res.status_code == 200, res.text

    res = client.get(f"/api/campaigns/{cid}/interview/candidates",
                     headers=admin_headers)
    assert res.status_code == 200, res.text
    item = [i for i in res.json() if i["student_id"] == "08269313"][0]
    assert item["booking"] == {"start_at": slot["start_at"],
                               "end_at": slot["end_at"], "note": slot["note"]}

    res = client.get(f"/api/campaigns/{cid}/applications/08269313/interview/brief",
                     headers=admin_headers)
    assert res.status_code == 200, res.text
    brief = res.json()
    assert brief["session_id"] == sid
    assert brief["booking"]["start_at"] == slot["start_at"]

    # 无预约、无会话的考生：booking 为 null、session_id 为 null
    _apply(client, cid, admin_headers, "08269314", status="shortlisted")
    res = client.get(f"/api/campaigns/{cid}/applications/08269314/interview/brief",
                     headers=admin_headers)
    assert res.status_code == 200, res.text
    brief = res.json()
    assert brief["booking"] is None
    assert brief["session_id"] is None
    res = client.get(f"/api/campaigns/{cid}/interview/candidates",
                     headers=admin_headers)
    item = [i for i in res.json() if i["student_id"] == "08269314"][0]
    assert item["booking"] is None
