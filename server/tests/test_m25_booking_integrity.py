"""面试预约查重与并发安全（M25）：
1) 同一考生同批次仅一个有效预约（重复预约别的槽 → 409 已有预约，请先取消）；
2) 同槽并发双抢仅一人成功（原子 UPDATE ... WHERE booked_by IS NULL + 唯一约束兜底）；
3) generate 幂等：重复 / 重叠窗口跳过已有起点，并发生成不造重复槽。
"""
import sqlite3
import threading
from datetime import date, timedelta

import pytest

from app import db
from conftest import applicant_headers

# 每个用例独立日期，避免共享测试库里的时段互相干扰（available 只返回未来时段）
D_SINGLE = (date.today() + timedelta(days=60)).isoformat()
D_SAME = (date.today() + timedelta(days=61)).isoformat()
D_RACE = (date.today() + timedelta(days=62)).isoformat()
D_SELF_RACE = (date.today() + timedelta(days=63)).isoformat()
D_IDEM = (date.today() + timedelta(days=64)).isoformat()
D_GEN_RACE = (date.today() + timedelta(days=65)).isoformat()


def _app_payload(student_id):
    return {
        "student_id": student_id,
        "name": f"M25考生{student_id[-2:]}",
        "gender": "male",
        "class_name": "计科244",
        "qq": "600600600",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "M25 预约查重测试",
    }


def _apply_shortlisted(client, cid, admin_headers, student_id):
    res = client.post(f"/api/campaigns/{cid}/applications",
                      json=_app_payload(student_id))
    assert res.status_code == 200, res.text
    res = client.patch(f"/api/campaigns/{cid}/applications/{student_id}/status",
                       json={"status": "shortlisted"}, headers=admin_headers)
    assert res.status_code == 200, res.text


def _generate(client, cid, admin_headers, day, windows, slot_minutes=45):
    res = client.post(
        f"/api/campaigns/{cid}/interview-slots/generate",
        json={"date": day, "windows": windows, "slot_minutes": slot_minutes},
        headers=admin_headers)
    assert res.status_code == 200, res.text
    return res.json()


def _free_slots(client, cid, admin_headers, day):
    res = client.get(f"/api/campaigns/{cid}/interview-slots", headers=admin_headers)
    assert res.status_code == 200, res.text
    free = [s for s in res.json()
            if s["start_at"].startswith(day) and s["booked_by"] is None]
    free.sort(key=lambda s: s["start_at"])
    return free


def _book(client, cid, headers, slot_id, method="online"):
    return client.post(f"/api/campaigns/{cid}/interview-slots/{slot_id}/book",
                       json={"method": method}, headers=headers)


def _slot_rows(cid, day):
    with db.connection() as conn:
        return conn.execute(
            "SELECT * FROM interview_slot"
            " WHERE campaign_id = ? AND substr(start_at, 1, 10) = ?"
            " ORDER BY start_at", (cid, day)).fetchall()


# ---------------------------------------------------------------------------
# 1) 同一考生同批次仅一个有效预约
# ---------------------------------------------------------------------------

def test_second_slot_conflict_409_and_db_constraint(client, cid, admin_headers):
    _generate(client, cid, admin_headers, D_SINGLE,
              [{"start": "10:00", "end": "12:00"}])
    free = _free_slots(client, cid, admin_headers, D_SINGLE)
    assert len(free) >= 2
    _apply_shortlisted(client, cid, admin_headers, "08269401")
    headers = applicant_headers(client, cid, "08269401")

    res = _book(client, cid, headers, free[0]["id"], method="offline")
    assert res.status_code == 200, res.text

    # 预约别的槽 → 409 冲突提示
    res = _book(client, cid, headers, free[1]["id"], method="online")
    assert res.status_code == 409
    assert "已有预约" in res.json()["detail"]
    assert "取消" in res.json()["detail"]

    # 库里仍只有一条有效预约
    booked = [r for r in _slot_rows(cid, D_SINGLE) if r["booked_by"] is not None]
    assert len(booked) == 1
    assert booked[0]["id"] == free[0]["id"]
    assert booked[0]["booked_by"] == "08269401"

    # 约束级别证明：绕过路由直插第二条同考生预约，被 UNIQUE (campaign_id, booked_by) 拒绝
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO interview_slot"
                " (id, campaign_id, start_at, end_at, note, booked_by, booked_at, created_at)"
                " VALUES ('m25dup01', ?, ?, ?, '', '08269401', 'x', 'x')",
                (cid, f"{D_SINGLE}T21:15:00+08:00", f"{D_SINGLE}T22:00:00+08:00"))


def test_rebook_same_slot_idempotent(client, cid, admin_headers):
    _generate(client, cid, admin_headers, D_SAME,
              [{"start": "10:00", "end": "12:00"}])
    free = _free_slots(client, cid, admin_headers, D_SAME)
    _apply_shortlisted(client, cid, admin_headers, "08269402")
    headers = applicant_headers(client, cid, "08269402")

    res = _book(client, cid, headers, free[0]["id"])
    assert res.status_code == 200, res.text
    # 双击 / 请求重放：同一格重复预约不报冲突，返回原预约
    res = _book(client, cid, headers, free[0]["id"])
    assert res.status_code == 200, res.text
    assert res.json()["id"] == free[0]["id"]
    booked = [r for r in _slot_rows(cid, D_SAME) if r["booked_by"] is not None]
    assert len(booked) == 1


# ---------------------------------------------------------------------------
# 2) 同槽并发双抢仅一人成功
# ---------------------------------------------------------------------------

def test_concurrent_double_book_single_winner(client, cid, admin_headers):
    _generate(client, cid, admin_headers, D_RACE,
              [{"start": "10:00", "end": "10:45"}])
    slot = _free_slots(client, cid, admin_headers, D_RACE)[0]
    _apply_shortlisted(client, cid, admin_headers, "08269403")
    _apply_shortlisted(client, cid, admin_headers, "08269404")
    h1 = applicant_headers(client, cid, "08269403")
    h2 = applicant_headers(client, cid, "08269404")

    barrier = threading.Barrier(2)
    results = {}

    def race(key, headers, method):
        barrier.wait()
        results[key] = _book(client, cid, headers, slot["id"], method=method)

    t1 = threading.Thread(target=race, args=("a", h1, "online"))
    t2 = threading.Thread(target=race, args=("b", h2, "offline"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], {k: v.text for k, v in results.items()}
    winner = next(k for k, r in results.items() if r.status_code == 200)
    loser = next(k for k, r in results.items() if r.status_code == 409)
    assert "抢走" in results[loser].json()["detail"]

    # 数据库最终态：该槽仅被一人持有，且 method 是胜者提交的那一个
    row = next(r for r in _slot_rows(cid, D_RACE) if r["id"] == slot["id"])
    winner_id = "08269403" if winner == "a" else "08269404"
    assert row["booked_by"] == winner_id
    assert row["method"] == ("online" if winner == "a" else "offline")
    booked = [r for r in _slot_rows(cid, D_RACE) if r["booked_by"] is not None]
    assert len(booked) == 1


def test_concurrent_self_double_book_single_row(client, cid, admin_headers):
    # 同一考生并发抢两个不同槽：UNIQUE (campaign_id, booked_by) 兜底，最终仅一条有效预约
    _generate(client, cid, admin_headers, D_SELF_RACE,
              [{"start": "10:00", "end": "12:00"}])
    free = _free_slots(client, cid, admin_headers, D_SELF_RACE)
    assert len(free) >= 2
    _apply_shortlisted(client, cid, admin_headers, "08269405")
    headers = applicant_headers(client, cid, "08269405")

    barrier = threading.Barrier(2)
    results = {}

    def race(key, slot_id):
        barrier.wait()
        results[key] = _book(client, cid, headers, slot_id)

    t1 = threading.Thread(target=race, args=("a", free[0]["id"]))
    t2 = threading.Thread(target=race, args=("b", free[1]["id"]))
    t1.start(); t2.start()
    t1.join(); t2.join()

    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], {k: v.text for k, v in results.items()}
    loser = next(r for r in results.values() if r.status_code == 409)
    assert "已有预约" in loser.json()["detail"]
    booked = [r for r in _slot_rows(cid, D_SELF_RACE) if r["booked_by"] is not None]
    assert len(booked) == 1


# ---------------------------------------------------------------------------
# 3) generate 幂等
# ---------------------------------------------------------------------------

def test_generate_idempotent_repeat_and_overlap(client, cid, admin_headers):
    # 首次：10:00-12:00 45 分钟 → 10:00 / 10:45
    out = _generate(client, cid, admin_headers, D_IDEM,
                    [{"start": "10:00", "end": "12:00"}])
    assert out["created"] == 2 and out["skipped"] == 0

    # 完全重放 → 全部跳过
    out = _generate(client, cid, admin_headers, D_IDEM,
                    [{"start": "10:00", "end": "12:00"}])
    assert out["created"] == 0 and out["skipped"] == 2

    # 子集窗口（10:45-11:30 → 起点 10:45）→ 已存在起点跳过
    out = _generate(client, cid, admin_headers, D_IDEM,
                    [{"start": "10:45", "end": "11:30"}])
    assert out["created"] == 0 and out["skipped"] == 1

    # 重叠窗口（11:00-13:30）：10:00/10:45 跳过，11:00 起新建 3 格
    out = _generate(client, cid, admin_headers, D_IDEM,
                    [{"start": "11:00", "end": "13:30"}])
    assert out["created"] == 3 and out["skipped"] == 0

    rows = _slot_rows(cid, D_IDEM)
    starts = [r["start_at"] for r in rows]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts)) == 5  # 不重复造槽


def test_generate_concurrent_no_duplicate_slots(client, cid, admin_headers):
    body = {"date": D_GEN_RACE, "windows": [{"start": "10:00", "end": "12:00"}],
            "slot_minutes": 45}
    barrier = threading.Barrier(2)
    outs = []

    def race():
        barrier.wait()
        res = client.post(f"/api/campaigns/{cid}/interview-slots/generate",
                          json=body, headers=admin_headers)
        assert res.status_code == 200, res.text
        outs.append(res.json())

    t1 = threading.Thread(target=race)
    t2 = threading.Thread(target=race)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 两次调用合计恰好人库 2 个槽，起点无重复（唯一索引兜底并发重放）
    assert sum(o["created"] for o in outs) == 2
    rows = _slot_rows(cid, D_GEN_RACE)
    starts = [r["start_at"] for r in rows]
    assert len(rows) == 2
    assert len(starts) == len(set(starts))

    # 约束级别证明：同批次同起点第二个直插被拒绝
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO interview_slot"
                " (id, campaign_id, start_at, end_at, note, created_at)"
                " VALUES ('m25dupslot', ?, ?, ?, '', 'x')",
                (cid, starts[0], starts[0]))
