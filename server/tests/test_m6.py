import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config, db, maintenance
from test_m3 import _enroll_draft, _upload

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _patch_status(client, admin_headers, cid, student_id, status):
    return client.patch(
        f"/api/campaigns/{cid}/applications/{student_id}/status",
        json={"status": status},
        headers=admin_headers,
    )


def _direct_application(cid, student_id, status="applied"):
    now = datetime.now(timezone.utc).isoformat()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO application (student_id, campaign_id, name, gender, class_name,"
            " qq, lab_intent, domains, focus_next_year, expectation, status,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, 'male', '计科244', '600600600', 1, '[\"llm_arch\"]',"
            " 'project', 'M6 测试', ?, ?, ?)",
            (student_id, cid, f"测试{student_id[-2:]}", status, now, now),
        )


def _direct_session(cid, student_id, sid):
    now = datetime.now(timezone.utc).isoformat()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO exam_session (id, campaign_id, student_id, session_token,"
            " machine_id, track, opened_at) VALUES (?, ?, ?, ?, ?, 'track1', ?)",
            (sid, cid, student_id, f"tok-{sid}", f"mid-{sid}", now),
        )


def test_patch_status_valid_flow(client, cid, admin_headers):
    student_id = "08269001"
    res = client.post(f"/api/campaigns/{cid}/applications", json={
        "student_id": student_id, "name": "流转甲", "gender": "male",
        "class_name": "计科244", "qq": "600600601", "lab_intent": True,
        "domains": ["llm_arch"], "focus_next_year": "project", "expectation": "",
    })
    assert res.status_code == 200
    before = res.json()

    res = _patch_status(client, admin_headers, cid, student_id, "shortlisted")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["status"] == "shortlisted"
    assert out["updated_at"] > before["updated_at"]
    assert out["created_at"] == before["created_at"]

    res = _patch_status(client, admin_headers, cid, student_id, "admitted")
    assert res.status_code == 200
    assert res.json()["status"] == "admitted"


def test_patch_status_invalid_enum_400(client, cid, admin_headers):
    res = _patch_status(client, admin_headers, cid, "08269001", "hired")
    assert res.status_code == 400


def test_patch_status_unknown_student_404(client, cid, admin_headers):
    res = _patch_status(client, admin_headers, cid, "08269999", "tested")
    assert res.status_code == 404


def test_patch_status_requires_admin(client, cid):
    res = client.patch(f"/api/campaigns/{cid}/applications/08269001/status",
                       json={"status": "tested"})
    assert res.status_code == 401


def test_upload_marks_application_tested(client, cid):
    student_id = "08269002"
    data = _enroll_draft(client, cid, student_id)
    with db.connection() as conn:
        status = conn.execute(
            "SELECT status FROM application WHERE campaign_id = ? AND student_id = ?",
            (cid, student_id),
        ).fetchone()["status"]
    assert status == "applied"

    res = _upload(client, cid, data["session_id"], token=data["session_token"])
    assert res.status_code == 200, res.text
    with db.connection() as conn:
        row = conn.execute(
            "SELECT status, updated_at FROM application"
            " WHERE campaign_id = ? AND student_id = ?",
            (cid, student_id),
        ).fetchone()
    assert row["status"] == "tested"
    assert row["updated_at"]


def test_notice_contains_admitted_only(client, cid, admin_headers):
    for student_id, status in (("08269003", "admitted"), ("08269004", "rejected")):
        client.post(f"/api/campaigns/{cid}/applications", json={
            "student_id": student_id,
            "name": f"公示{'录' if status == 'admitted' else '拒'}",
            "gender": "female", "class_name": "计科245", "qq": "600600602",
            "lab_intent": False, "domains": ["agent"],
            "focus_next_year": "research", "expectation": "",
        })
        assert _patch_status(client, admin_headers, cid, student_id,
                             status).status_code == 200

    res = client.get(f"/api/campaigns/{cid}/export/notice", headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/plain")
    assert res.headers["content-type"].endswith("charset=utf-8")
    assert res.headers["content-disposition"] == f'attachment; filename="notice_{cid}.txt"'
    text = res.content.decode("utf-8")
    assert "公示录（计科245）" in text          # admitted 姓名 + 班级
    assert "公示拒" not in text                 # rejected 不出现
    assert "2026 春季招新" in text             # 批次名
    assert "公示日期：" in text                 # 落款日期

    res = client.get(f"/api/campaigns/{cid}/export/notice")
    assert res.status_code == 401


def _direct_score(sid):
    now = datetime.now(timezone.utc).isoformat()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO exam_score (session_id, increment_score, human_rate,"
            " comment, scorer, created_at, updated_at)"
            " VALUES (?, 3.5, 4.0, 'M6 回收测试', 'admin', ?, ?)",
            (sid, now, now),
        )


def test_purge_expired_idempotent():
    cid = "purge2099"
    sid = "sess-purge-1"
    now = datetime.now(timezone.utc)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO campaign (id, name, year, status, expires_at, created_at)"
            " VALUES (?, '回收测试批次', 2099, 'open', ?, ?)",
            (cid, (now - timedelta(days=1)).isoformat(), now.isoformat()),
        )
    _direct_application(cid, "08269005")
    _direct_session(cid, "08269005", sid)
    _direct_score(sid)
    uploads_dir = Path(config.DATA_DIR) / "uploads" / cid / sid
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / "cleaned.jsonl").write_text("{}\n", encoding="utf-8")

    summary = maintenance.purge_expired(now_iso_str=now.isoformat())
    assert cid in summary["purged"]
    assert cid not in summary["skipped"]
    assert not uploads_dir.exists()
    assert not (Path(config.DATA_DIR) / "uploads" / cid).exists()

    with db.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM exam_session WHERE campaign_id = ?", (cid,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM exam_score WHERE session_id = ?", (sid,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM application WHERE campaign_id = ?", (cid,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT status FROM campaign WHERE id = ?", (cid,)
        ).fetchone()["status"] == "closed"

    # 重跑幂等：已回收批次进入 skipped，状态与行数不变
    summary2 = maintenance.purge_expired(now_iso_str=now.isoformat())
    assert cid in summary2["skipped"]
    assert cid not in summary2["purged"]
    with db.connection() as conn:
        assert conn.execute(
            "SELECT status FROM campaign WHERE id = ?", (cid,)
        ).fetchone()["status"] == "closed"
        assert conn.execute(
            "SELECT COUNT(*) c FROM application WHERE campaign_id = ?", (cid,)
        ).fetchone()["c"] == 0
        assert not (Path(config.DATA_DIR) / "uploads" / cid).exists()

    # 清理测试批次行，避免影响其他用例
    with db.connection() as conn:
        conn.execute("DELETE FROM campaign WHERE id = ?", (cid,))
