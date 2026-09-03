import io
import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from .. import db
from ..auth import require_admin
from ..constants import MAX_DOMAINS
from ..schemas import ApplicationCreate, ApplicationOut, domain_labels, focus_label, gender_label
from .campaigns import get_campaign_or_404, now_iso

router = APIRouter(prefix="/api/campaigns", tags=["applications"])


def resolve_application(conn: sqlite3.Connection, cid: str, student_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM application WHERE campaign_id = ? AND student_id = ?",
        (cid, student_id),
    ).fetchone()


def _row_to_out(row: sqlite3.Row) -> ApplicationOut:
    return ApplicationOut(
        student_id=row["student_id"],
        campaign_id=row["campaign_id"],
        name=row["name"],
        gender=row["gender"],
        class_name=row["class_name"],
        qq=row["qq"],
        lab_intent=bool(row["lab_intent"]),
        domains=json.loads(row["domains"]),
        focus_next_year=row["focus_next_year"],
        expectation=row["expectation"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("/{cid}/applications", response_model=ApplicationOut)
def submit_application(cid: str, body: ApplicationCreate):
    if len(body.domains) > MAX_DOMAINS:
        raise HTTPException(status_code=400, detail=f"领域最多选择 {MAX_DOMAINS} 个")
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        existing = resolve_application(conn, cid, body.student_id)
        now = now_iso()
        params = {
            "student_id": body.student_id,
            "campaign_id": cid,
            "name": body.name,
            "gender": body.gender,
            "class_name": body.class_name,
            "qq": body.qq,
            "lab_intent": int(body.lab_intent),
            "domains": json.dumps(body.domains, ensure_ascii=False),
            "focus_next_year": body.focus_next_year,
            "expectation": body.expectation,
            "updated_at": now,
        }
        if existing is not None:
            if not body.overwrite:
                raise HTTPException(
                    status_code=409,
                    detail={"message": "该学号已有报名记录", "student_id": body.student_id,
                            "existing_name": existing["name"]},
                )
            params["created_at"] = existing["created_at"]
            conn.execute(
                "UPDATE application SET name=:name, gender=:gender, class_name=:class_name,"
                " qq=:qq, lab_intent=:lab_intent, domains=:domains,"
                " focus_next_year=:focus_next_year, expectation=:expectation,"
                " updated_at=:updated_at"
                " WHERE campaign_id=:campaign_id AND student_id=:student_id",
                params,
            )
        else:
            params["created_at"] = now
            conn.execute(
                "INSERT INTO application (student_id, campaign_id, name, gender, class_name,"
                " qq, lab_intent, domains, focus_next_year, expectation, status,"
                " created_at, updated_at)"
                " VALUES (:student_id, :campaign_id, :name, :gender, :class_name,"
                " :qq, :lab_intent, :domains, :focus_next_year, :expectation, 'applied',"
                " :created_at, :updated_at)",
                params,
            )
        row = resolve_application(conn, cid, body.student_id)
        return _row_to_out(row)


@router.get("/{cid}/applications/export.csv")
def export_applications_csv(
    cid: str,
    lab_intent: bool | None = None,
    status: str | None = None,
    _: None = Depends(require_admin),
):
    with db.connection() as conn:
        rows = query_applications(conn, cid, lab_intent, status)
    buffer = io.StringIO()
    buffer.write("\ufeff")
    header = ["学号", "姓名", "性别", "班级", "QQ", "实验室意向", "领域", "明年重心",
              "期望收获", "状态", "创建时间", "更新时间"]
    buffer.write(",".join(_csv_escape(h) for h in header) + "\r\n")
    for row in rows:
        a = _row_to_out(row)
        fields = [
            a.student_id,
            a.name,
            gender_label(a.gender),
            a.class_name,
            a.qq,
            "是" if a.lab_intent else "否",
            "；".join(domain_labels(a.domains)),
            focus_label(a.focus_next_year),
            a.expectation,
            a.status,
            a.created_at,
            a.updated_at,
        ]
        buffer.write(",".join(_csv_escape(f) for f in fields) + "\r\n")
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="applications_{cid}.csv"'},
    )


@router.get("/{cid}/applications/{student_id}", response_model=ApplicationOut)
def get_application(cid: str, student_id: str):
    with db.connection() as conn:
        row = resolve_application(conn, cid, student_id)
        if row is None:
            raise HTTPException(status_code=404, detail="未找到该学号的报名记录")
        return _row_to_out(row)


def query_applications(
    conn: sqlite3.Connection,
    cid: str,
    lab_intent: bool | None = None,
    status: str | None = None,
    q: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM application WHERE campaign_id = ?"
    params: list = [cid]
    if lab_intent is not None:
        sql += " AND lab_intent = ?"
        params.append(int(lab_intent))
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND (student_id LIKE ? OR name LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like])
    sql += " ORDER BY created_at DESC"
    return conn.execute(sql, params).fetchall()


@router.get("/{cid}/applications", response_model=list[ApplicationOut])
def list_applications(
    cid: str,
    lab_intent: bool | None = None,
    status: str | None = None,
    q: str | None = None,
    _: None = Depends(require_admin),
):
    with db.connection() as conn:
        rows = query_applications(conn, cid, lab_intent, status, q)
        return [_row_to_out(r) for r in rows]


def _csv_escape(value: str) -> str:
    value = (value or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if any(c in value for c in ',"\n\r'):
        return '"' + value.replace('"', '""') + '"'
    return value
