import json
import secrets
import sqlite3
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException

from .. import db
from ..auth import require_admin
from ..constants import EXAM_TRACKS
from ..db import DEFAULT_EXAM_CONFIG
from ..schemas import ExamEnroll, ExamSessionOut, ExamVoid
from .campaigns import get_campaign_or_404, now_iso

router = APIRouter(prefix="/api/campaigns", tags=["exam"])

TRACK_KEYS = [t["key"] for t in EXAM_TRACKS]


def exam_config_for(row: sqlite3.Row) -> dict:
    cfg = {}
    try:
        cfg = json.loads(row["exam_config"] or "{}")
    except ValueError:
        cfg = {}
    merged = dict(DEFAULT_EXAM_CONFIG)
    merged.update(cfg)
    return merged


def pick_questions(conn: sqlite3.Connection, domains: list[str], track: str,
                   per_domain: int) -> list[sqlite3.Row]:
    picked: list[sqlite3.Row] = []
    for domain in domains:
        rows = conn.execute(
            "SELECT * FROM question WHERE domain = ? AND track = ? AND active = 1"
            " ORDER BY RANDOM() LIMIT ?",
            (domain, track, per_domain),
        ).fetchall()
        picked.extend(rows)
    return picked


def questions_to_dict(rows: list[sqlite3.Row]) -> list[dict]:
    return [{"id": r["id"], "domain": r["domain"], "title": r["title"], "body": r["body"]}
            for r in rows]


def get_session_or_404(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM exam_session WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="考试会话不存在")
    return row


def _row_to_out(row: sqlite3.Row) -> ExamSessionOut:
    return ExamSessionOut(
        id=row["id"],
        campaign_id=row["campaign_id"],
        student_id=row["student_id"],
        machine_id=row["machine_id"],
        track=row["track"],
        domain_tags=json.loads(row["domain_tags"]),
        questions_served=json.loads(row["questions_served"]),
        rerolls=row["rerolls"],
        status=row["status"],
        opened_at=row["opened_at"],
        uploaded_at=row["uploaded_at"],
        student_name=row["student_name"] if "student_name" in row.keys() else None,
    )


def _serve_response(row: sqlite3.Row, cfg: dict) -> dict:
    qs = json.loads(row["questions_served"])
    track = row["track"]
    guide = cfg.get("track3_guide_text", "") if track == "track3" else ""
    return {
        "session_id": row["id"],
        "session_token": row["session_token"],
        "machine_id": row["machine_id"],
        "track": track,
        "domain_tags": json.loads(row["domain_tags"]),
        "questions": qs,
        "track3_guide_text": guide,
        "rerolls_left": max(0, cfg["reroll_limit"] - row["rerolls"]),
    }


@router.post("/{cid}/exam/enroll")
def enroll(cid: str, body: ExamEnroll):
    if body.track not in TRACK_KEYS:
        raise HTTPException(status_code=400, detail=f"无效轨迹: {body.track}")
    with db.connection() as conn:
        campaign = get_campaign_or_404(conn, cid)
        if campaign["status"] != "open":
            raise HTTPException(status_code=403, detail="当前批次已收口，无法开考")
        app = conn.execute(
            "SELECT * FROM application WHERE campaign_id = ? AND student_id = ?",
            (cid, body.student_id),
        ).fetchone()
        if app is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "no_application",
                        "message": "未找到该学号的报名记录，请先填写报名问卷（支持现场补录）"},
            )
        existing = conn.execute(
            "SELECT * FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " AND status = 'draft' ORDER BY opened_at DESC LIMIT 1",
            (cid, body.student_id),
        ).fetchone()
        if existing is not None:
            cfg = exam_config_for(campaign)
            raise HTTPException(
                status_code=409,
                detail={"code": "already_enrolled",
                        "message": "已存在未交卷的考试会话，如需重考请由监考授权作废",
                        "session_id": existing["id"],
                        **{k: v for k, v in _serve_response(existing, cfg).items()}},
            )
        prev_uploaded = conn.execute(
            "SELECT 1 FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " AND status = 'uploaded'",
            (cid, body.student_id),
        ).fetchone()
        if prev_uploaded is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "already_uploaded",
                        "message": "该学号已交卷，请勿重复开考"},
            )
        cfg = exam_config_for(campaign)
        domains = json.loads(app["domains"])
        per_domain = cfg["track1_per_domain"] if body.track == "track1" else cfg["track2_per_domain"]
        qrows = [] if body.track == "track3" else pick_questions(conn, domains, body.track, per_domain)
        sid = uuid.uuid4().hex[:16]
        token = secrets.token_urlsafe(32)
        machine_id = uuid.uuid4().hex[:16]
        now = now_iso()
        conn.execute(
            "INSERT INTO exam_session (id, campaign_id, student_id, session_token,"
            " machine_id, track, domain_tags, questions_served, status, opened_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
            (sid, cid, body.student_id, token, machine_id, body.track,
             json.dumps(domains, ensure_ascii=False),
             json.dumps(questions_to_dict(qrows), ensure_ascii=False), now),
        )
        row = get_session_or_404(conn, sid)
        return _serve_response(row, cfg)


@router.post("/{cid}/exam/reroll")
def reroll(cid: str, body: dict, x_session_token: str | None = Header(default=None)):
    sid = str(body.get("session_id", ""))
    if not sid or not x_session_token:
        raise HTTPException(status_code=400, detail="缺少会话凭证")
    with db.connection() as conn:
        campaign = get_campaign_or_404(conn, cid)
        row = get_session_or_404(conn, sid)
        if row["campaign_id"] != cid:
            raise HTTPException(status_code=404, detail="会话不属于该批次")
        if row["session_token"] != x_session_token:
            raise HTTPException(status_code=401, detail="会话凭证无效")
        if row["status"] != "draft":
            raise HTTPException(status_code=409, detail="会话已交卷，不能换题")
        cfg = exam_config_for(campaign)
        if row["rerolls"] >= cfg["reroll_limit"]:
            raise HTTPException(status_code=409, detail=f"已用完换题次数（{cfg['reroll_limit']} 次）")
        track = row["track"]
        if track == "track3":
            raise HTTPException(status_code=400, detail="自由创作型轨迹无需换题")
        domains = json.loads(row["domain_tags"])
        per_domain = cfg["track1_per_domain"] if track == "track1" else cfg["track2_per_domain"]
        qrows = pick_questions(conn, domains, track, per_domain)
        conn.execute(
            "UPDATE exam_session SET questions_served = ?, rerolls = rerolls + 1 WHERE id = ?",
            (json.dumps(questions_to_dict(qrows), ensure_ascii=False), sid),
        )
        updated = get_session_or_404(conn, sid)
        return _serve_response(updated, cfg)


@router.post("/{cid}/exam/void", response_model=ExamSessionOut)
def void_sessions(cid: str, body: ExamVoid, _: None = Depends(require_admin)):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        conn.execute(
            "UPDATE exam_session SET status = 'voided'"
            " WHERE campaign_id = ? AND student_id = ? AND status = 'draft'",
            (cid, body.student_id),
        )
        row = conn.execute(
            "SELECT * FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " AND status = 'voided' ORDER BY opened_at DESC LIMIT 1",
            (cid, body.student_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="该学号没有可作废的考试会话")
        return _row_to_out(row)


@router.get("/{cid}/exam/sessions", response_model=list[ExamSessionOut])
def list_sessions(cid: str, student_id: str | None = None, _: None = Depends(require_admin)):
    sql = ("SELECT s.*, a.name AS student_name FROM exam_session s"
           " LEFT JOIN application a ON s.campaign_id = a.campaign_id"
           " AND s.student_id = a.student_id WHERE s.campaign_id = ?")
    params: list = [cid]
    if student_id:
        sql += " AND s.student_id = ?"
        params.append(student_id)
    sql += " ORDER BY s.opened_at DESC"
    with db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_out(r) for r in rows]
