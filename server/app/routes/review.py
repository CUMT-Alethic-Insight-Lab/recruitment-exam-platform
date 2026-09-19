import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..auth import require_admin
from ..schemas import ReviewSessionOut, ScoreIn, ScoreOut
from .campaigns import get_campaign_or_404, now_iso

router = APIRouter(prefix="/api/campaigns", tags=["review"], dependencies=[Depends(require_admin)])


def _get_session(conn: sqlite3.Connection, cid: str, sid: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM exam_session WHERE id = ? AND campaign_id = ?", (sid, cid)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="考试会话不存在")
    return row


def _question_meta(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    row = conn.execute(
        "SELECT questions_served FROM exam_session WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return []
    try:
        served = json.loads(row["questions_served"] or "[]")
    except ValueError:
        return []
    items = []
    for q in served:
        qrow = conn.execute(
            "SELECT bank_id, title, role, meta FROM question WHERE id = ?",
            (q.get("id"),),
        ).fetchone()
        if qrow is None:
            continue
        try:
            meta = json.loads(qrow["meta"] or "{}")
        except ValueError:
            meta = {}
        items.append({
            "bank_id": qrow["bank_id"],
            "title": qrow["title"],
            "role": qrow["role"],
            "failure_modes": meta.get("failure_modes"),
            "pass_signals": meta.get("pass_signals"),
            "reviewer_notes": meta.get("reviewer_notes"),
        })
    return items


@router.get("/{cid}/review/sessions", response_model=list[ReviewSessionOut])
def list_review_sessions(cid: str):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        rows = conn.execute(
            """
            SELECT s.id AS session_id, s.student_id, s.track, s.uploaded_at, s.repo_url,
                   (s.cleaned_file_path IS NULL) AS stored_lan,
                   a.name AS student_name,
                   sc.increment_score, sc.human_rate, sc.scorer, sc.comment,
                   sc.updated_at AS score_updated_at
            FROM exam_session s
            JOIN application a
                ON a.campaign_id = s.campaign_id AND a.student_id = s.student_id
            LEFT JOIN exam_score sc ON sc.session_id = s.id
            WHERE s.campaign_id = ? AND s.status = 'uploaded'
            ORDER BY s.uploaded_at DESC
            """,
            (cid,),
        ).fetchall()
        items = []
        for r in rows:
            item = ReviewSessionOut(
                **dict(r), question_meta=_question_meta(conn, r["session_id"])
            )
            items.append(item)
        return items


@router.get("/{cid}/sessions/{sid}/cleaned")
def get_cleaned(cid: str, sid: str):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        session = _get_session(conn, cid, sid)
        if session["status"] != "uploaded" or not session["cleaned_file_path"]:
            raise HTTPException(status_code=404, detail="该会话没有已上传的 cleaned 文件")
        path = Path(session["cleaned_file_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="cleaned 文件不存在")
        items = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items


@router.post("/{cid}/sessions/{sid}/score", response_model=ScoreOut)
def submit_score(cid: str, sid: str, body: ScoreIn):
    if not (0 <= body.increment_score <= 100):
        raise HTTPException(status_code=400, detail="increment_score 需在 0-100 之间")
    if not (0 <= body.human_rate <= 100):
        raise HTTPException(status_code=400, detail="human_rate 需在 0-100 之间")
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        session = _get_session(conn, cid, sid)
        if session["status"] != "uploaded":
            raise HTTPException(status_code=409, detail="该会话未交卷，不能打分")
        now = now_iso()
        existing = conn.execute(
            "SELECT created_at FROM exam_score WHERE session_id = ?", (sid,)
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        conn.execute(
            """
            INSERT OR REPLACE INTO exam_score
                (session_id, increment_score, human_rate, comment, scorer,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, body.increment_score, body.human_rate, body.comment, body.scorer,
             created_at, now),
        )
        row = conn.execute("SELECT * FROM exam_score WHERE session_id = ?", (sid,)).fetchone()
        return ScoreOut(**dict(row))
