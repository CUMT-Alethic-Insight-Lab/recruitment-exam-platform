import json
import sqlite3
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import db
from ..auth import require_admin
from ..constants import DOMAIN_KEYS, EXAM_TRACKS
from ..schemas import QuestionCreate, QuestionOut, QuestionPatch
from .campaigns import now_iso

router = APIRouter(prefix="/api/questions", tags=["questions"])

TRACK_KEYS = [t["key"] for t in EXAM_TRACKS]


def _row_to_out(row: sqlite3.Row) -> QuestionOut:
    return QuestionOut(
        id=row["id"],
        domain=row["domain"],
        track=row["track"],
        title=row["title"],
        body=row["body"],
        tags=json.loads(row["tags"]),
        active=bool(row["active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate(body_domain: str, body_track: str) -> None:
    if body_domain not in DOMAIN_KEYS:
        raise HTTPException(status_code=400, detail=f"无效领域: {body_domain}")
    if body_track not in TRACK_KEYS:
        raise HTTPException(status_code=400, detail=f"无效轨迹: {body_track}")


@router.post("", response_model=QuestionOut, status_code=201)
def create_question(body: QuestionCreate, _: None = Depends(require_admin)):
    _validate(body.domain, body.track)
    qid = uuid.uuid4().hex[:16]
    now = now_iso()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO question (id, domain, track, title, body, tags, active,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (qid, body.domain, body.track, body.title, body.body,
             json.dumps(body.tags, ensure_ascii=False), int(body.active), now, now),
        )
        row = conn.execute("SELECT * FROM question WHERE id = ?", (qid,)).fetchone()
        return _row_to_out(row)


@router.get("", response_model=list[QuestionOut])
def list_questions(
    domain: str | None = None,
    track: str | None = None,
    active: bool | None = None,
    q: str | None = None,
    _: None = Depends(require_admin),
):
    sql = "SELECT * FROM question WHERE 1=1"
    params: list = []
    if domain:
        sql += " AND domain = ?"
        params.append(domain)
    if track:
        sql += " AND track = ?"
        params.append(track)
    if active is not None:
        sql += " AND active = ?"
        params.append(int(active))
    if q:
        sql += " AND (title LIKE ? OR body LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like])
    sql += " ORDER BY domain, track, created_at"
    with db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_out(r) for r in rows]


@router.patch("/{qid}", response_model=QuestionOut)
def patch_question(qid: str, body: QuestionPatch, _: None = Depends(require_admin)):
    updates = []
    params: list = []
    for key in ("domain", "track", "title", "body", "tags", "active"):
        val = getattr(body, key)
        if val is not None:
            updates.append(f"{key} = ?")
            params.append(json.dumps(val, ensure_ascii=False) if key == "tags" else val)
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM question WHERE id = ?", (qid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="题目不存在")
        if body.domain is not None and body.domain not in DOMAIN_KEYS:
            raise HTTPException(status_code=400, detail=f"无效领域: {body.domain}")
        if body.track is not None and body.track not in TRACK_KEYS:
            raise HTTPException(status_code=400, detail=f"无效轨迹: {body.track}")
        if not updates:
            return _row_to_out(row)
        params.append(now_iso())
        params.append(qid)
        conn.execute(
            f"UPDATE question SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
            params,
        )
        row = conn.execute("SELECT * FROM question WHERE id = ?", (qid,)).fetchone()
        return _row_to_out(row)


@router.delete("/{qid}")
def delete_question(qid: str, _: None = Depends(require_admin)):
    with db.connection() as conn:
        cur = conn.execute("DELETE FROM question WHERE id = ?", (qid,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="题目不存在")
    return {"ok": True}
