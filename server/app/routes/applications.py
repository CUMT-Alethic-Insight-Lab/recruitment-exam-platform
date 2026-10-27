import io
import json
import sqlite3
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from .. import auth, captcha, db
from ..auth import require_admin, require_applicant
from ..constants import APPLICATION_STATUS, MAX_DOMAINS
from ..schemas import (
    ApplicationCreate,
    ApplicationOut,
    RegistrationCreate,
    RegistrationOut,
    domain_labels,
    focus_label,
    gender_label,
)
from .campaigns import ensure_apply_open, get_campaign_or_404, now_iso

router = APIRouter(prefix="/api/campaigns", tags=["applications"])

# 学号已存在账号/已完成注册时的统一冲突文案（M26：注册冲突覆盖漏洞）
CONFLICT_REGISTERED = "该学号已注册，请直接登录或联系管理员重置密码"


def resolve_application(conn: sqlite3.Connection, cid: str, student_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM application WHERE campaign_id = ? AND student_id = ?",
        (cid, student_id),
    ).fetchone()


def _ensure_account(conn: sqlite3.Connection, student_id: str, name: str,
                    salt: str, stored_hash: str, now: str) -> None:
    """新建报名时同步建 account 行（默认密码=学号，昵称=姓名）；已有账号则不动。"""
    conn.execute(
        "INSERT OR IGNORE INTO account"
        " (student_id, role, nickname, pw_salt, pw_hash, pw_changed, created_at, updated_at)"
        " VALUES (?, 'applicant', ?, ?, ?, 0, ?, ?)",
        (student_id, name, salt, stored_hash, now, now),
    )


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
        exam_mode=row["exam_mode"],
        registration_complete=row["registration_complete"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("/{cid}/applications", response_model=ApplicationOut)
def submit_application(cid: str, body: ApplicationCreate, request: Request):
    if len(body.domains) > MAX_DOMAINS:
        raise HTTPException(status_code=400, detail=f"领域最多选择 {MAX_DOMAINS} 个")
    captcha.verify_or_http(body.captcha_id, body.captcha_text)
    with db.connection() as conn:
        campaign = get_campaign_or_404(conn, cid)
        ensure_apply_open(campaign)
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
            "exam_mode": body.exam_mode,
            "updated_at": now,
        }
        if existing is not None:
            if not body.overwrite:
                raise HTTPException(
                    status_code=409,
                    detail={"message": "该学号已有报名记录", "student_id": body.student_id,
                            "existing_name": existing["name"]},
                )
            if existing["registration_complete"]:
                # 已完成注册的报名不允许匿名/他人覆盖（M26）：问卷提交端点本身匿名可达，
                # 必须校验持有人会话，防止凭学号直接改写他人（含管理员）档案
                sess = auth.lookup_session(conn, auth.bearer_token(request))
                if sess is None or sess["kind"] not in ("applicant", "member") \
                        or (sess["campaign_id"] != cid and not (
                            sess["kind"] == "member" and sess["campaign_id"] is None)):
                    raise HTTPException(
                        status_code=401,
                        detail="该学号已完成注册，请先登录本人账号后再修改报名信息",
                    )
                if sess["student_id"] != body.student_id:
                    raise HTTPException(
                        status_code=403, detail="只能修改本人的报名信息")
            params["created_at"] = existing["created_at"]
            conn.execute(
                "UPDATE application SET name=:name, gender=:gender, class_name=:class_name,"
                " qq=:qq, lab_intent=:lab_intent, domains=:domains,"
                " focus_next_year=:focus_next_year, expectation=:expectation,"
                " exam_mode=:exam_mode, registration_complete=1, updated_at=:updated_at"
                " WHERE campaign_id=:campaign_id AND student_id=:student_id",
                params,
            )
        else:
            if conn.execute("SELECT 1 FROM account WHERE student_id = ?",
                            (body.student_id,)).fetchone() is not None:
                # 已有账号（管理员/往届批次考生）不得再经报名端点建档案（M26 顶号漏洞）
                raise HTTPException(status_code=409, detail=CONFLICT_REGISTERED)
            params["created_at"] = now
            salt = auth.new_salt()
            client_hash = auth.compute_client_hash(body.student_id, salt)
            stored_hash = auth.store_hash(client_hash)
            conn.execute(
                "INSERT INTO application (student_id, campaign_id, name, gender, class_name,"
                " qq, lab_intent, domains, focus_next_year, expectation, exam_mode,"
                " registration_complete, status, pw_salt, pw_hash, pw_changed, created_at, updated_at)"
                " VALUES (:student_id, :campaign_id, :name, :gender, :class_name,"
                " :qq, :lab_intent, :domains, :focus_next_year, :expectation, :exam_mode,"
                " 1, 'applied', :pw_salt, :pw_hash, 0, :created_at, :updated_at)",
                {**params, "pw_salt": salt, "pw_hash": stored_hash},
            )
            _ensure_account(conn, body.student_id, body.name, salt, stored_hash, now)
        row = resolve_application(conn, cid, body.student_id)
        return _row_to_out(row)


@router.post("/{cid}/applications/register", response_model=RegistrationOut)
def register(cid: str, body: RegistrationCreate):
    """流程式注册第一步：建号（未完成）或续填（幂等），完成后需提交完整问卷。"""
    with db.connection() as conn:
        campaign = get_campaign_or_404(conn, cid)
        ensure_apply_open(campaign)
    captcha.verify_or_http(body.captcha_id, body.captcha_text)
    with db.connection() as conn:
        existing = resolve_application(conn, cid, body.student_id)
        now = now_iso()
        if existing is not None and existing["registration_complete"]:
            raise HTTPException(status_code=409, detail=CONFLICT_REGISTERED)
        if existing is None and conn.execute(
                "SELECT 1 FROM account WHERE student_id = ?",
                (body.student_id,)).fetchone() is not None:
            # 学号已建账号（管理员/往届批次考生）但本批次无报名行：
            # 拒绝匿名重复注册，防止凭他人学号顶建档案（M26 顶号漏洞）
            raise HTTPException(status_code=409, detail=CONFLICT_REGISTERED)
        if existing is None:
            salt = auth.new_salt()
            client_hash = auth.compute_client_hash(body.student_id, salt)
            stored_hash = auth.store_hash(client_hash)
            conn.execute(
                "INSERT INTO application (student_id, campaign_id, name, gender, class_name,"
                " qq, lab_intent, domains, focus_next_year, expectation, exam_mode,"
                " registration_complete, status, pw_salt, pw_hash, pw_changed,"
                " created_at, updated_at)"
                " VALUES (:student_id, :campaign_id, :name, :gender, '', '', 0, '[]', '', '',"
                " 'standard', 0, 'applied', :pw_salt, :pw_hash, 0, :now, :now)",
                {"student_id": body.student_id, "campaign_id": cid,
                 "name": body.name, "gender": body.gender,
                 "pw_salt": salt, "pw_hash": stored_hash, "now": now},
            )
            _ensure_account(conn, body.student_id, body.name, salt, stored_hash, now)
            message = "注册成功，请继续完成报名问卷（初始密码为您的学号）"
        else:
            conn.execute(
                "UPDATE application SET name = ?, gender = ?, updated_at = ?"
                " WHERE campaign_id = ? AND student_id = ?",
                (body.name, body.gender, now, cid, body.student_id),
            )
            message = "注册信息已更新，请继续完成报名问卷（初始密码为您的学号）"
        row = resolve_application(conn, cid, body.student_id)
        return RegistrationOut(
            student_id=row["student_id"],
            campaign_id=cid,
            name=row["name"],
            gender=row["gender"],
            registration_complete=row["registration_complete"],
            message=message,
        )


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


class ApplicationStatusPatch(BaseModel):
    status: str


@router.patch("/{cid}/applications/{student_id}/status", response_model=ApplicationOut)
def patch_application_status(
    cid: str,
    student_id: str,
    body: ApplicationStatusPatch,
    _: None = Depends(require_admin),
):
    if body.status not in APPLICATION_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"无效状态，须为 {'/'.join(APPLICATION_STATUS)} 之一",
        )
    with db.connection() as conn:
        row = resolve_application(conn, cid, student_id)
        if row is None:
            raise HTTPException(status_code=404, detail="未找到该学号的报名记录")
        conn.execute(
            "UPDATE application SET status = ?, updated_at = ?"
            " WHERE campaign_id = ? AND student_id = ?",
            (body.status, now_iso(), cid, student_id),
        )
        row = resolve_application(conn, cid, student_id)
        return _row_to_out(row)


NOTICE_TITLE = "Alethic Insight 社团招新录取公示"


@router.get("/{cid}/export/notice")
def export_admission_notice(cid: str, _: None = Depends(require_admin)):
    with db.connection() as conn:
        campaign = get_campaign_or_404(conn, cid)
        rows = conn.execute(
            "SELECT * FROM application WHERE campaign_id = ? AND status = 'admitted'"
            " ORDER BY student_id",
            (cid,),
        ).fetchall()
    lines = [
        NOTICE_TITLE,
        "",
        f"批次名称：{campaign['name']}",
        "",
        "经机试与评审，现将本批次录取名单公示如下：",
        "",
    ]
    if rows:
        lines.extend(
            f"{i}. {r['name']}（{r['class_name']}）" for i, r in enumerate(rows, 1)
        )
    else:
        lines.append("（本批次暂无录取名单）")
    today = datetime.now()
    lines.extend([
        "",
        f"公示日期：{today.year}年{today.month}月{today.day}日",
        "Alethic Insight 社团",
    ])
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="notice_{cid}.txt"'},
    )


@router.get("/{cid}/applications/{student_id}", response_model=ApplicationOut)
def get_application(
    cid: str,
    student_id: str,
    request: Request,
):
    # 权限：管理员，或考生本人（Bearer 会话且 student_id 匹配）——档案含姓名/QQ/班级等个人信息
    applicant_id = None
    try:
        applicant_id = require_applicant(request, cid)
    except HTTPException:
        require_admin(request)
    if applicant_id is not None and applicant_id != student_id:
        raise HTTPException(status_code=403, detail="只能查看本人报名档案")
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
