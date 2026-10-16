import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db
from ..auth import require_admin, require_applicant
from ..schemas import AdminSlotOut, CandidateSlotOut, SlotBookIn, SlotGenerateIn
from .applications import resolve_application
from .campaigns import get_campaign_or_404, now_iso

# 管理端写接口走统一 admin 门禁；考生端接口在函数内用 require_applicant(request, cid)
admin_router = APIRouter(
    prefix="/api/campaigns", tags=["interview-slots"],
    dependencies=[Depends(require_admin)],
)
router = APIRouter(prefix="/api/campaigns", tags=["interview-slots"])

# 面试时段固定按北京时间（+08:00）生成与存储
TZ_CN = timezone(timedelta(hours=8))

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

SLOT_MIN_MINUTES = 15
SLOT_MAX_MINUTES = 120

NOT_ELIGIBLE = "当前状态不可预约面试"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _admin_slot(conn: sqlite3.Connection, cid: str, slot_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.*, a.name AS student_name FROM interview_slot s"
        " LEFT JOIN application a"
        "   ON a.campaign_id = s.campaign_id AND a.student_id = s.booked_by"
        " WHERE s.id = ? AND s.campaign_id = ?",
        (slot_id, cid),
    ).fetchone()


def _my_booking(conn: sqlite3.Connection, cid: str, student_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM interview_slot WHERE campaign_id = ? AND booked_by = ?",
        (cid, student_id),
    ).fetchone()


@admin_router.post("/{cid}/interview-slots/generate")
def generate_slots(cid: str, body: SlotGenerateIn):
    if not (SLOT_MIN_MINUTES <= body.slot_minutes <= SLOT_MAX_MINUTES):
        raise HTTPException(
            status_code=400,
            detail=f"slot_minutes 需在 {SLOT_MIN_MINUTES}-{SLOT_MAX_MINUTES} 分钟之间")
    try:
        day = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date 格式应为 YYYY-MM-DD")
    windows: list[tuple[datetime, datetime]] = []
    for w in body.windows:
        if not (_HHMM.match(w.start) and _HHMM.match(w.end)):
            raise HTTPException(status_code=400, detail="时间格式应为 HH:MM")
        sh, sm = (int(x) for x in w.start.split(":"))
        eh, em = (int(x) for x in w.end.split(":"))
        start_dt = datetime.combine(day, time(sh, sm), TZ_CN)
        end_dt = datetime.combine(day, time(eh, em), TZ_CN)
        if start_dt >= end_dt:
            raise HTTPException(status_code=400, detail="窗口开始时间需早于结束时间")
        windows.append((start_dt, end_dt))
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        existing = {
            r["start_at"]
            for r in conn.execute(
                "SELECT start_at FROM interview_slot WHERE campaign_id = ?", (cid,))
        }
        created = 0
        skipped = 0
        now = now_iso()
        step = timedelta(minutes=body.slot_minutes)
        for start_dt, end_dt in windows:
            cur = start_dt
            while cur + step <= end_dt:
                start_at = cur.isoformat()
                if start_at in existing:
                    skipped += 1
                else:
                    try:
                        conn.execute(
                            "INSERT INTO interview_slot"
                            " (id, campaign_id, start_at, end_at, note, created_at)"
                            " VALUES (?, ?, ?, ?, ?, ?)",
                            (uuid.uuid4().hex[:16], cid, start_at,
                             (cur + step).isoformat(), body.note, now),
                        )
                    except sqlite3.IntegrityError:
                        # 唯一索引 (campaign_id, start_at) 兜底：并发生成时撞线视为跳过
                        skipped += 1
                    else:
                        existing.add(start_at)
                        created += 1
                cur += step
        rows = conn.execute(
            "SELECT s.*, a.name AS student_name FROM interview_slot s"
            " LEFT JOIN application a"
            "   ON a.campaign_id = s.campaign_id AND a.student_id = s.booked_by"
            " WHERE s.campaign_id = ? AND substr(s.start_at, 1, 10) = ?"
            " ORDER BY s.start_at",
            (cid, body.date),
        ).fetchall()
        return {
            "created": created,
            "skipped": skipped,
            "slots": [AdminSlotOut(**dict(r)) for r in rows],
        }


@admin_router.get("/{cid}/interview-slots", response_model=list[AdminSlotOut])
def list_slots(cid: str):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        rows = conn.execute(
            "SELECT s.*, a.name AS student_name FROM interview_slot s"
            " LEFT JOIN application a"
            "   ON a.campaign_id = s.campaign_id AND a.student_id = s.booked_by"
            " WHERE s.campaign_id = ? ORDER BY s.start_at",
            (cid,),
        ).fetchall()
        return [AdminSlotOut(**dict(r)) for r in rows]


@admin_router.delete("/{cid}/interview-slots/{slot_id}")
def delete_slot(cid: str, slot_id: str):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        row = _admin_slot(conn, cid, slot_id)
        if row is None:
            raise HTTPException(status_code=404, detail="面试时段不存在")
        if row["booked_by"] is not None:
            raise HTTPException(status_code=409, detail="该时段已有预约，请先取消预约")
        conn.execute("DELETE FROM interview_slot WHERE id = ?", (slot_id,))
        return {"ok": True}


@admin_router.post("/{cid}/interview-slots/{slot_id}/unbook", response_model=AdminSlotOut)
def unbook_slot(cid: str, slot_id: str):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        row = _admin_slot(conn, cid, slot_id)
        if row is None:
            raise HTTPException(status_code=404, detail="面试时段不存在")
        conn.execute(
            "UPDATE interview_slot"
            " SET booked_by = NULL, booked_at = NULL, booked_mode = NULL WHERE id = ?",
            (slot_id,),
        )
        return AdminSlotOut(**dict(_admin_slot(conn, cid, slot_id)))


@router.get("/{cid}/interview-slots/available")
def available_slots(cid: str, request: Request):
    student_id = require_applicant(request, cid)
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        app = resolve_application(conn, cid, student_id)
        mine = _my_booking(conn, cid, student_id)
        if (app is None or app["status"] != "shortlisted") and mine is None:
            raise HTTPException(status_code=403, detail=NOT_ELIGIBLE)
        now = _utcnow()
        rows = conn.execute(
            "SELECT * FROM interview_slot WHERE campaign_id = ? ORDER BY start_at",
            (cid,),
        ).fetchall()
        slots = [
            {
                "id": r["id"],
                "start_at": r["start_at"],
                "end_at": r["end_at"],
                "note": r["note"],
                "taken": r["booked_by"] is not None,
            }
            for r in rows
            if _parse_at(r["start_at"]) > now
        ]
        return {
            "eligible": True,
            "my_booking": CandidateSlotOut(**dict(mine)) if mine is not None else None,
            "slots": slots,
        }


@router.post("/{cid}/interview-slots/{slot_id}/book", response_model=CandidateSlotOut)
def book_slot(cid: str, slot_id: str, request: Request, body: SlotBookIn):
    student_id = require_applicant(request, cid)
    if body.mode not in ("online", "offline"):
        raise HTTPException(status_code=400, detail="请选择线上或线下面试")
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        app = resolve_application(conn, cid, student_id)
        if app is None or app["status"] != "shortlisted":
            raise HTTPException(status_code=403, detail=NOT_ELIGIBLE)
        slot = conn.execute(
            "SELECT * FROM interview_slot WHERE id = ? AND campaign_id = ?",
            (slot_id, cid),
        ).fetchone()
        if slot is None:
            raise HTTPException(status_code=404, detail="面试时段不存在")
        if slot["booked_by"] == student_id:
            # 幂等重放：重复预约自己已持有的同一格（双击/重试）不算改约冲突
            return CandidateSlotOut(**dict(slot))
        if _parse_at(slot["start_at"]) <= _utcnow():
            raise HTTPException(status_code=400, detail="该时段已开始，无法预约")
        now = now_iso()
        try:
            cur = conn.execute(
                "UPDATE interview_slot SET booked_by = ?, booked_at = ?, booked_mode = ?"
                " WHERE id = ? AND booked_by IS NULL",
                (student_id, now, body.mode, slot_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="你已有预约，请先取消再改约")
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail="该时段刚被抢走，请刷新重选")
        row = conn.execute(
            "SELECT * FROM interview_slot WHERE id = ?", (slot_id,)).fetchone()
        return CandidateSlotOut(**dict(row))


@router.post("/{cid}/interview-slots/mine/cancel")
def cancel_my_slot(cid: str, request: Request):
    student_id = require_applicant(request, cid)
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        mine = _my_booking(conn, cid, student_id)
        if mine is None:
            raise HTTPException(status_code=404, detail="你当前没有预约")
        if _parse_at(mine["start_at"]) <= _utcnow():
            raise HTTPException(
                status_code=400, detail="时段已开始，无法取消，如需改约请联系管理员")
        conn.execute(
            "UPDATE interview_slot"
            " SET booked_by = NULL, booked_at = NULL, booked_mode = NULL WHERE id = ?",
            (mine["id"],),
        )
        return {"ok": True}
