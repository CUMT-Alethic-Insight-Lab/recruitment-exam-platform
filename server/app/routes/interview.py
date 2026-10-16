import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..auth import require_admin
from ..schemas import InterviewOut, InterviewUpsert
from .applications import resolve_application
from .campaigns import get_campaign_or_404, now_iso

router = APIRouter(
    prefix="/api/campaigns", tags=["interview"], dependencies=[Depends(require_admin)]
)

# 加权总分权重（M1 30% M2 20% M3 15% M4 20% M5 15%），满分 5
INTERVIEW_WEIGHTS = {"m1": 0.30, "m2": 0.20, "m3": 0.15, "m4": 0.20, "m5": 0.15}

S_FLAG_LEVEL = "待定（S档疑点，进入复审）"

# 只列这些状态的报名（排除 applied）
CANDIDATE_STATUSES = ("tested", "shortlisted", "interviewed", "admitted", "rejected")

SAFETY_NOTE = (
    "安全抽题规则：会话领域含 agent 时建议抽 S1，其余建议抽 S3；"
    "附三安全素材不进机试下发，仅供面试提问使用。"
)


def weighted_total(m: dict) -> float:
    return round(sum(m[k] * w for k, w in INTERVIEW_WEIGHTS.items()), 4)


def auto_level(total: float, s_flag: bool) -> str:
    if s_flag:
        return S_FLAG_LEVEL
    if total >= 4.2:
        return "强推"
    if total >= 3.5:
        return "推荐"
    if total >= 2.8:
        return "待定"
    return "不推荐"


def _row_to_out(row: sqlite3.Row) -> InterviewOut:
    return InterviewOut(
        campaign_id=row["campaign_id"],
        student_id=row["student_id"],
        m1=row["m1"],
        m2=row["m2"],
        m3=row["m3"],
        m4=row["m4"],
        m5=row["m5"],
        evidence=json.loads(row["evidence"] or "{}"),
        track_source=row["track_source"],
        s_flag=row["s_flag"],
        inconsistency=row["inconsistency"],
        direct_channel=row["direct_channel"],
        free_note=row["free_note"],
        level=row["level"],
        weighted_total=row["weighted_total"],
        interviewer=row["interviewer"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _get_interview(conn: sqlite3.Connection, cid: str, sid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM interview_score WHERE campaign_id = ? AND student_id = ?",
        (cid, sid),
    ).fetchone()


def _latest_live_session(conn: sqlite3.Connection, cid: str, sid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM exam_session WHERE campaign_id = ? AND student_id = ?"
        " AND status != 'voided' ORDER BY opened_at DESC, id DESC LIMIT 1",
        (cid, sid),
    ).fetchone()


def _booking_for(conn: sqlite3.Connection, cid: str, sid: str) -> dict | None:
    """本人面试时段预约（过期的也带上，面试当天要看）。"""
    row = conn.execute(
        "SELECT start_at, end_at, note FROM interview_slot"
        " WHERE campaign_id = ? AND booked_by = ?",
        (cid, sid),
    ).fetchone()
    if row is None:
        return None
    return {"start_at": row["start_at"], "end_at": row["end_at"], "note": row["note"]}


def _k3_for_session(conn: sqlite3.Connection, session_id: str | None) -> dict | None:
    if session_id is None:
        return None
    row = conn.execute(
        "SELECT increment_score, human_rate, comment, scorer FROM exam_score"
        " WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "increment_score": row["increment_score"],
        "human_rate": row["human_rate"],
        "comment": row["comment"],
        "scorer": row["scorer"],
    }


def _candidate_common(conn: sqlite3.Connection, cid: str, sid: str,
                      with_interview: bool = True) -> dict:
    app = resolve_application(conn, cid, sid)
    if app is None:
        raise HTTPException(status_code=404, detail="未找到该学号的报名记录")
    session = _latest_live_session(conn, cid, sid)
    track = session["track"] if session is not None else None
    k3 = _k3_for_session(conn, session["id"] if session is not None else None)
    out = {
        "student_id": sid,
        "name": app["name"],
        "status": app["status"],
        "domains": json.loads(app["domains"]),
        "track": track,
        "k3": k3,
        "booking": _booking_for(conn, cid, sid),
    }
    if with_interview:
        iv = _get_interview(conn, cid, sid)
        out["interview"] = _row_to_out(iv) if iv is not None else None
    return out


@router.get("/{cid}/interview/candidates")
def list_interview_candidates(cid: str):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        rows = conn.execute(
            "SELECT student_id FROM application WHERE campaign_id = ?"
            f" AND status IN ({','.join('?' * len(CANDIDATE_STATUSES))})"
            " ORDER BY student_id",
            (cid, *CANDIDATE_STATUSES),
        ).fetchall()
        return [_candidate_common(conn, cid, r["student_id"]) for r in rows]


@router.get("/{cid}/applications/{sid}/interview", response_model=InterviewOut)
def get_interview(cid: str, sid: str):
    with db.connection() as conn:
        row = _get_interview(conn, cid, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="该考生尚无面试记录")
        return _row_to_out(row)


@router.put("/{cid}/applications/{sid}/interview", response_model=InterviewOut)
def upsert_interview(cid: str, sid: str, body: InterviewUpsert):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        app = resolve_application(conn, cid, sid)
        if app is None:
            raise HTTPException(status_code=404, detail="未找到该学号的报名记录")
        existing = _get_interview(conn, cid, sid)

        def pick(field: str, default):
            provided = getattr(body, field)
            if provided is not None:
                return provided
            if existing is not None:
                return existing[field]
            return default

        m = {"m1": body.m1, "m2": body.m2, "m3": body.m3,
             "m4": body.m4, "m5": body.m5}
        total = weighted_total(m)
        s_flag = int(pick("s_flag", False))
        explicit_level = (body.level or "").strip()
        level = explicit_level if explicit_level else auto_level(total, bool(s_flag))
        if body.evidence is not None:
            evidence = body.evidence
        elif existing is not None:
            evidence = json.loads(existing["evidence"] or "{}")
        else:
            evidence = {}
        now = now_iso()
        created_at = existing["created_at"] if existing is not None else now
        conn.execute(
            """
            INSERT OR REPLACE INTO interview_score
                (campaign_id, student_id, m1, m2, m3, m4, m5, evidence,
                 track_source, s_flag, inconsistency, direct_channel, free_note,
                 level, weighted_total, interviewer, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, sid, body.m1, body.m2, body.m3, body.m4, body.m5,
             json.dumps(evidence, ensure_ascii=False),
             pick("track_source", "A"),
             s_flag,
             pick("inconsistency", ""),
             int(pick("direct_channel", False)),
             pick("free_note", ""),
             level,
             total,
             pick("interviewer", ""),
             created_at, now),
        )
        if app["status"] not in ("admitted", "rejected"):
            conn.execute(
                "UPDATE application SET status = 'interviewed', updated_at = ?"
                " WHERE campaign_id = ? AND student_id = ?",
                (now, cid, sid),
            )
        return _row_to_out(_get_interview(conn, cid, sid))


def _meta_of(row: sqlite3.Row) -> dict:
    try:
        return json.loads(row["meta"] or "{}")
    except ValueError:
        return {}


@router.get("/{cid}/applications/{sid}/interview/brief")
def interview_brief(cid: str, sid: str):
    with db.connection() as conn:
        common = _candidate_common(conn, cid, sid, with_interview=False)
        session = _latest_live_session(conn, cid, sid)
        questions = []
        domain_tags: list[str] = []
        if session is not None:
            domain_tags = json.loads(session["domain_tags"] or "[]")
            served = json.loads(session["questions_served"] or "[]")
            for q in served:
                row = conn.execute(
                    "SELECT * FROM question WHERE id = ?", (q.get("id"),)
                ).fetchone()
                meta = _meta_of(row) if row is not None else {}
                questions.append({
                    "bank_id": row["bank_id"] if row is not None else None,
                    "title": row["title"] if row is not None else q.get("title", ""),
                    "role": row["role"] if row is not None else "",
                    "domain": row["domain"] if row is not None else q.get("domain", ""),
                    "body": row["body"] if row is not None else q.get("body", ""),
                    "basis": meta.get("basis"),
                    "test_points": meta.get("test_points"),
                    "pass_signals": meta.get("pass_signals"),
                    "failure_modes": meta.get("failure_modes"),
                    "reviewer_notes": meta.get("reviewer_notes"),
                })
        safety_rows = conn.execute(
            "SELECT bank_id, title, body, meta FROM question"
            " WHERE domain = 'ai_safety' AND active = 0 ORDER BY bank_id"
        ).fetchall()
        safety_picks = [
            {
                "bank_id": r["bank_id"],
                "title": r["title"],
                "body": r["body"],
                "reviewer_notes": _meta_of(r).get("reviewer_notes"),
            }
            for r in safety_rows
        ]
        return {
            **common,
            "session_id": session["id"] if session is not None else None,
            "questions": questions,
            "safety_picks": safety_picks,
            "suggested_safety_pick": "S1" if "agent" in domain_tags else "S3",
            "note": SAFETY_NOTE,
        }
