import json
import uuid

import pytest

from app import db
from conftest import applicant_headers

CLEANED_CONTENT = '{"event": "kimi_code_session", "turns": [1]}\n'

# 只出现在 meta 里的标记串，用于钉死"绝不泄漏评审专用字段"
SECRET_MARKER = "M5_SECRET_reviewer_notes"

AGENT_META = {
    "basis": "出题依据-AGENT",
    "test_points": ["考察点1", "考察点2"],
    "pass_signals": ["能说清编排边界"],
    "failure_modes": ["只会背概念"],
    "reviewer_notes": SECRET_MARKER,
}


def _app_payload(student_id, domains=None):
    return {
        "student_id": student_id,
        "name": f"M5考生{student_id[-2:]}",
        "gender": "male",
        "class_name": "计科244",
        "qq": "600600600",
        "lab_intent": True,
        "domains": domains or ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "M5 面试评分测试",
    }


def _apply(client, cid, admin_headers, student_id, domains=None, status=None):
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload(student_id, domains))
    assert res.status_code == 200, res.text
    if status is not None:
        res = client.patch(f"/api/campaigns/{cid}/applications/{student_id}/status",
                           json={"status": status}, headers=admin_headers)
        assert res.status_code == 200, res.text


def _insert_question(domain, track, bank_id, role="正选",
                     active=1, meta=None, title=None):
    qid = uuid.uuid4().hex[:16]
    now = "2026-09-04T00:00:00+00:00"
    assert bank_id in M5_BANK_IDS, "测试插入的题目必须登记在 M5_BANK_IDS 里以便清理"
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO question (id, domain, track, title, body, tags, active,"
            " bank_id, role, meta, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)",
            (qid, domain, track, title or f"{bank_id} 题面",
             f"{bank_id} 的题面正文（不含评审内容）", active, bank_id, role,
             json.dumps(meta if meta is not None else AGENT_META, ensure_ascii=False),
             now, now),
        )
    return qid


# 本模块插入的题目 bank_id（避免与题库 v5 正式 bank_id 冲突），
# 每个测试结束后统一清理，保证不污染共享测试库里的题库导入计数。
M5_BANK_IDS = ("Q-AGENT-M5", "Q-AIX-M5", "Q-LEAK-M5", "S1-M5T", "S3-M5T")


@pytest.fixture(autouse=True)
def _cleanup_inserted_questions():
    yield
    with db.connection() as conn:
        conn.execute(
            f"DELETE FROM question WHERE bank_id IN ({','.join('?' * len(M5_BANK_IDS))})",
            M5_BANK_IDS,
        )


def _enroll(client, cid, student_id, track="track1"):
    headers = applicant_headers(client, cid, student_id)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": track}, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _enroll_and_upload(client, cid, student_id, track="track1"):
    data = _enroll(client, cid, student_id, track)
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


def _put_interview(client, cid, admin_headers, student_id, **overrides):
    scores = {"m1": 4, "m2": 4, "m3": 4, "m4": 4, "m5": 4}
    scores.update(overrides)
    return client.put(f"/api/campaigns/{cid}/applications/{student_id}/interview",
                      json=scores, headers=admin_headers)


# ---------------------------------------------------------------------------
# 契约 1：GET /interview/candidates
# ---------------------------------------------------------------------------

def test_interview_endpoints_require_admin(client, cid):
    res = client.get(f"/api/campaigns/{cid}/interview/candidates")
    assert res.status_code == 401
    res = client.put(f"/api/campaigns/{cid}/applications/08269101/interview",
                     json={"m1": 3, "m2": 3, "m3": 3, "m4": 3, "m5": 3})
    assert res.status_code == 401
    res = client.get(f"/api/campaigns/{cid}/applications/08269101/interview/brief")
    assert res.status_code == 401
    res = client.get(f"/api/campaigns/{cid}/applications/08269101/interview")
    assert res.status_code == 401


def test_candidates_list_fields(client, cid, admin_headers):
    # 无会话考生：track/k3/interview 均为 null
    _apply(client, cid, admin_headers, "08269101", status="tested")
    # 有会话且已打分考生：track/k3 有值
    _apply(client, cid, admin_headers, "08269102", status="shortlisted")
    sid = _enroll_and_upload(client, cid, "08269102")
    res = client.post(f"/api/campaigns/{cid}/sessions/{sid}/score",
                      json={"increment_score": 88, "human_rate": 90,
                            "comment": "机试表现好", "scorer": "评委A"},
                      headers=admin_headers)
    assert res.status_code == 200
    # applied 状态不进列表
    _apply(client, cid, admin_headers, "08269103")

    res = client.get(f"/api/campaigns/{cid}/interview/candidates",
                     headers=admin_headers)
    assert res.status_code == 200, res.text
    items = {i["student_id"]: i for i in res.json()}
    assert "08269103" not in items

    c1 = items["08269101"]
    assert c1["name"] == "M5考生01"
    assert c1["status"] == "tested"
    assert c1["domains"] == ["llm_arch"]
    assert c1["track"] is None
    assert c1["k3"] is None
    assert c1["interview"] is None

    c2 = items["08269102"]
    assert c2["track"] == "track1"
    assert c2["k3"] == {"increment_score": 88, "human_rate": 90,
                        "comment": "机试表现好", "scorer": "评委A"}
    assert c2["interview"] is None


def test_candidates_track_uses_latest_nonvoid_session(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269104", status="tested")
    _enroll(client, cid, "08269104", track="track1")
    res = client.post(f"/api/campaigns/{cid}/exam/void",
                      json={"student_id": "08269104"}, headers=admin_headers)
    assert res.status_code == 200
    _enroll(client, cid, "08269104", track="track2")

    res = client.get(f"/api/campaigns/{cid}/interview/candidates",
                     headers=admin_headers)
    item = [i for i in res.json() if i["student_id"] == "08269104"][0]
    assert item["track"] == "track2"


# ---------------------------------------------------------------------------
# 契约 2/3：GET / PUT interview
# ---------------------------------------------------------------------------

def test_get_interview_404(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269105", status="tested")
    res = client.get(f"/api/campaigns/{cid}/applications/08269105/interview",
                     headers=admin_headers)
    assert res.status_code == 404
    assert res.json()["detail"] == "该考生尚无面试记录"


def test_put_auto_level_tiers_and_status_side_effect(client, cid, admin_headers):
    tiers = [
        ("08269106", 4.5, 4.5, "强推"),
        ("08269107", 4.0, 4.0, "推荐"),
        ("08269108", 3.0, 3.0, "待定"),
        ("08269109", 2.0, 2.0, "不推荐"),
    ]
    for student, m, total, level in tiers:
        _apply(client, cid, admin_headers, student, status="shortlisted")
        res = _put_interview(client, cid, admin_headers, student, **{f"m{i}": m for i in range(1, 6)})
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["level"] == level
        assert out["weighted_total"] == total
        # 副作用：shortlisted → interviewed
        app = client.get(f"/api/campaigns/{cid}/applications/{student}", headers=admin_headers).json()
        assert app["status"] == "interviewed"

    # admitted/rejected 状态不被改写
    _apply(client, cid, admin_headers, "08269110", status="admitted")
    res = _put_interview(client, cid, admin_headers, "08269110")
    assert res.status_code == 200
    app = client.get(f"/api/campaigns/{cid}/applications/08269110", headers=admin_headers).json()
    assert app["status"] == "admitted"

    # GET 回读一致
    res = client.get(f"/api/campaigns/{cid}/applications/08269106/interview",
                     headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["level"] == "强推"
    assert res.json()["weighted_total"] == 4.5


def test_put_s_flag_overrides_level(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269111", status="tested")
    res = _put_interview(client, cid, admin_headers, "08269111",
                         **{f"m{i}": 5 for i in range(1, 6)}, s_flag=True,
                         inconsistency="k3 分数与表现明显不符")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["s_flag"] == 1
    assert out["weighted_total"] == 5.0
    assert out["level"] == "待定（S档疑点，进入复审）"
    assert out["inconsistency"] == "k3 分数与表现明显不符"


def test_put_explicit_level_wins_then_recompute_on_resubmit(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269112", status="tested")
    res = _put_interview(client, cid, admin_headers, "08269112",
                         **{f"m{i}": 2 for i in range(1, 6)}, level="特批观察")
    assert res.status_code == 200
    assert res.json()["level"] == "特批观察"

    # 再次提交未显式给 level → 按新分数自动重算
    res = _put_interview(client, cid, admin_headers, "08269112",
                         **{f"m{i}": 3 for i in range(1, 6)})
    assert res.status_code == 200
    assert res.json()["level"] == "待定"


def test_put_upsert_preserves_optional_fields(client, cid, admin_headers):
    student = "08269113"
    _apply(client, cid, admin_headers, student, status="tested")
    res = _put_interview(client, cid, admin_headers, student,
                         evidence={"m1": "答出了 KV cache 量化权衡"},
                         track_source="B", free_note="沟通能力好",
                         interviewer="评委X", direct_channel=True)
    assert res.status_code == 200, res.text
    first = res.json()
    assert first["track_source"] == "B"
    assert first["direct_channel"] == 1
    assert first["evidence"] == {"m1": "答出了 KV cache 量化权衡"}

    res = _put_interview(client, cid, admin_headers, student, **{f"m{i}": 5 for i in range(1, 6)})
    second = res.json()
    assert second["track_source"] == "B"
    assert second["direct_channel"] == 1
    assert second["free_note"] == "沟通能力好"
    assert second["interviewer"] == "评委X"
    assert second["evidence"] == {"m1": "答出了 KV cache 量化权衡"}
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]
    assert second["weighted_total"] == 5.0
    assert second["level"] == "强推"


def test_put_validation_422(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269114", status="tested")
    base = {f"m{i}": 3 for i in range(1, 6)}
    # 不允许 0.5 以外的步进
    res = client.put(f"/api/campaigns/{cid}/applications/08269114/interview",
                     json={**base, "m1": 4.3}, headers=admin_headers)
    assert res.status_code == 422
    # 越界
    res = client.put(f"/api/campaigns/{cid}/applications/08269114/interview",
                     json={**base, "m1": 0.5}, headers=admin_headers)
    assert res.status_code == 422
    res = client.put(f"/api/campaigns/{cid}/applications/08269114/interview",
                     json={**base, "m5": 5.5}, headers=admin_headers)
    assert res.status_code == 422
    # 缺字段
    res = client.put(f"/api/campaigns/{cid}/applications/08269114/interview",
                     json={k: v for k, v in base.items() if k != "m3"},
                     headers=admin_headers)
    assert res.status_code == 422
    # track_source 只允许 A/B
    res = client.put(f"/api/campaigns/{cid}/applications/08269114/interview",
                     json={**base, "track_source": "C"}, headers=admin_headers)
    assert res.status_code == 422
    # 0.5 步进是合法的
    res = client.put(f"/api/campaigns/{cid}/applications/08269114/interview",
                     json={**base, "m2": 3.5}, headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["m2"] == 3.5


def test_put_unknown_student_404(client, cid, admin_headers):
    res = client.put(f"/api/campaigns/{cid}/applications/08269999/interview",
                     json={f"m{i}": 3 for i in range(1, 6)}, headers=admin_headers)
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# 契约 4：GET brief
# ---------------------------------------------------------------------------

def test_brief_questions_and_safety_picks(client, cid, admin_headers):
    _insert_question("agent", "track1", "Q-AGENT-M5", role="正选")
    _insert_question("ai_safety", "track1", "S1-M5T", role="加试",
                     active=0, meta={"reviewer_notes": "S1 追问提示：多智能体失控场景"},
                     title="S1 安全追问")
    _insert_question("ai_safety", "track1", "S3-M5T", role="加试",
                     active=0, meta={"reviewer_notes": "S3 追问提示：数据安全"},
                     title="S3 安全追问")

    # 会话领域含 agent → 建议 S1
    _apply(client, cid, admin_headers, "08269121", domains=["agent", "llm_arch"],
           status="tested")
    _enroll(client, cid, "08269121", track="track1")
    res = client.get(f"/api/campaigns/{cid}/applications/08269121/interview/brief",
                     headers=admin_headers)
    assert res.status_code == 200, res.text
    brief = res.json()
    assert brief["student_id"] == "08269121"
    assert brief["track"] == "track1"
    assert brief["suggested_safety_pick"] == "S1"
    assert brief["note"]
    qs = [q for q in brief["questions"] if q["bank_id"] == "Q-AGENT-M5"]
    assert len(qs) == 1
    q = qs[0]
    assert q["role"] == "正选"
    assert q["domain"] == "agent"
    assert "题面正文" in q["body"]
    assert q["basis"] == "出题依据-AGENT"
    assert q["test_points"] == ["考察点1", "考察点2"]
    assert q["pass_signals"] == ["能说清编排边界"]
    assert q["failure_modes"] == ["只会背概念"]
    assert q["reviewer_notes"] == SECRET_MARKER

    picks = {p["bank_id"]: p for p in brief["safety_picks"]}
    assert "S1-M5T" in picks and "S3-M5T" in picks
    assert picks["S1-M5T"]["reviewer_notes"].startswith("S1 追问提示")
    assert "追问提示" in json.dumps(brief, ensure_ascii=False)

    # 无 agent 领域 → 建议 S3
    _apply(client, cid, admin_headers, "08269122", domains=["memory"],
           status="tested")
    _enroll(client, cid, "08269122", track="track1")
    res = client.get(f"/api/campaigns/{cid}/applications/08269122/interview/brief",
                     headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["suggested_safety_pick"] == "S3"


def test_brief_without_session(client, cid, admin_headers):
    _apply(client, cid, admin_headers, "08269123", status="tested")
    res = client.get(f"/api/campaigns/{cid}/applications/08269123/interview/brief",
                     headers=admin_headers)
    assert res.status_code == 200
    brief = res.json()
    assert brief["track"] is None
    assert brief["questions"] == []
    assert brief["suggested_safety_pick"] == "S3"
    res = client.get(f"/api/campaigns/{cid}/applications/08269998/interview/brief",
                     headers=admin_headers)
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# 契约 5：review/sessions 的 question_meta
# ---------------------------------------------------------------------------

def test_review_sessions_question_meta(client, cid, admin_headers):
    # 用 memory：pytest 按文件名顺序执行时，本用例之前无 memory/track1 活跃题，
    # 随机发题必然命中本用例插入的题（aix 裁撤前的等价确定性）
    _insert_question("memory", "track1", "Q-AIX-M5", role="正选",
                     meta=AGENT_META)

    _apply(client, cid, admin_headers, "08269131", domains=["memory"],
           status="tested")
    sid1 = _enroll_and_upload(client, cid, "08269131")
    _apply(client, cid, admin_headers, "08269132", status="tested")
    sid2 = _enroll_and_upload(client, cid, "08269132")

    # 第二个考生的会话引用了 question 表中不存在的题目 id → question_meta 为空数组
    with db.connection() as conn:
        conn.execute(
            "UPDATE exam_session SET questions_served = ? WHERE id = ?",
            (json.dumps([{"id": "bogus-not-in-question-table", "domain": "llm_arch",
                          "title": "占位", "body": "占位"}]), sid2),
        )

    res = client.get(f"/api/campaigns/{cid}/review/sessions", headers=admin_headers)
    assert res.status_code == 200, res.text
    items = {s["session_id"]: s for s in res.json()}
    meta1 = items[sid1]["question_meta"]
    assert len(meta1) >= 1
    target = [m for m in meta1 if m["bank_id"] == "Q-AIX-M5"]
    assert len(target) == 1
    assert target[0]["role"] == "正选"
    assert target[0]["title"] == "Q-AIX-M5 题面"
    assert target[0]["failure_modes"] == ["只会背概念"]
    assert target[0]["pass_signals"] == ["能说清编排边界"]
    assert target[0]["reviewer_notes"] == SECRET_MARKER
    assert items[sid2]["question_meta"] == []


# ---------------------------------------------------------------------------
# 契约 6：机试下发绝不携带 meta/reviewer_notes
# ---------------------------------------------------------------------------

def test_enroll_reroll_and_sessions_no_meta_leak(client, cid, admin_headers):
    # 用 memory：保证随机发题命中本用例插入的题，泄漏检查才有意义（aix 裁撤前的等价确定性）
    _insert_question("memory", "track1", "Q-LEAK-M5", role="正选",
                     meta=AGENT_META, title="Q-LEAK-M5 干净题面")

    _apply(client, cid, admin_headers, "08269141", domains=["memory"],
           status="tested")
    data = _enroll(client, cid, "08269141", track="track1")
    assert len(data["questions"]) >= 1
    for q in data["questions"]:
        assert set(q.keys()) == {"id", "domain", "title", "body"}
    assert SECRET_MARKER not in json.dumps(data, ensure_ascii=False)
    assert "出题依据" not in json.dumps(data, ensure_ascii=False)

    # reroll 同样不泄漏
    headers = applicant_headers(client, cid, "08269141")
    res = client.post(f"/api/campaigns/{cid}/exam/reroll",
                      json={"session_id": data["session_id"]},
                      headers={**headers, "X-Session-Token": data["session_token"]})
    assert res.status_code == 200, res.text
    rerolled = res.json()
    for q in rerolled["questions"]:
        assert set(q.keys()) == {"id", "domain", "title", "body"}
    assert SECRET_MARKER not in json.dumps(rerolled, ensure_ascii=False)

    # 管理端会话列表（含 questions_served）也不泄漏
    res = client.get(f"/api/campaigns/{cid}/exam/sessions",
                     params={"student_id": "08269141"}, headers=admin_headers)
    assert res.status_code == 200
    text = json.dumps(res.json(), ensure_ascii=False)
    assert SECRET_MARKER not in text
    assert "出题依据" not in text
