import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, db
from ..auth import require_admin
from ..studentid import parse_student_id

router = APIRouter(prefix="/api/admin/accounts", tags=["accounts"])

admin_router = APIRouter(prefix="/api/admin", tags=["accounts"])


@admin_router.get("/audit_log")
def list_audit_log(_: None = Depends(require_admin)):
    """管理操作审计（最近 200 条，只读；展示 UI 后续再说）。"""
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


class RoleBody(BaseModel):
    role: str


def _current_admin(request: Request) -> sqlite3.Row:
    return auth.require_admin_session(request)


@router.get("")
def list_accounts(
    role: str | None = None,
    q: str | None = None,
    _: None = Depends(require_admin),
):
    with db.connection() as conn:
        accounts = conn.execute(
            "SELECT * FROM account ORDER BY created_at DESC, student_id"
        ).fetchall()
        apps = conn.execute(
            "SELECT student_id, name, registration_complete FROM application"
            " ORDER BY created_at DESC"
        ).fetchall()
    latest_app = {}
    for row in apps:
        latest_app.setdefault(row["student_id"], row)
    items = []
    for acct in accounts:
        if role and acct["role"] != role:
            continue
        app_row = latest_app.get(acct["student_id"])
        parsed = parse_student_id(acct["student_id"])
        item = {
            "student_id": acct["student_id"],
            "name": app_row["name"] if app_row is not None else "",
            "nickname": acct["nickname"],
            "role": acct["role"],
            "dept": parsed["dept"],
            "enroll_year": parsed["enroll_year"],
            "pw_changed": bool(acct["pw_changed"]),
            "registration_complete": bool(app_row["registration_complete"]) if app_row is not None else False,
            "created_at": acct["created_at"],
        }
        if q:
            needle = q.lower()
            if not (needle in acct["student_id"].lower()
                    or needle in item["name"].lower()
                    or needle in item["nickname"].lower()):
                continue
        items.append(item)
    return {"items": items}


@router.post("/{student_id}/role")
def set_role(student_id: str, body: RoleBody, request: Request):
    sess = _current_admin(request)
    if body.role not in ("admin", "applicant"):
        raise HTTPException(status_code=400, detail="无效角色，须为 admin/applicant")
    with db.connection() as conn:
        acct = conn.execute(
            "SELECT * FROM account WHERE student_id = ?", (student_id,)
        ).fetchone()
        if acct is None:
            raise HTTPException(status_code=404, detail="未找到账号")
        if body.role == "applicant" and acct["role"] == "admin":
            if student_id == sess["student_id"]:
                raise HTTPException(status_code=400, detail="不能降级当前登录的管理员账号")
            admin_count = conn.execute(
                "SELECT COUNT(*) c FROM account WHERE role = 'admin'"
            ).fetchone()["c"]
            if admin_count <= 1:
                raise HTTPException(status_code=400, detail="系统至少需要保留一名管理员")
        conn.execute(
            "UPDATE account SET role = ?, updated_at = ? WHERE student_id = ?",
            (body.role, auth._utcnow().isoformat(), student_id),
        )
        auth.write_audit(conn, sess["student_id"], "set_role", student_id,
                         f"{acct['role']} -> {body.role}")
    return {"ok": True}


@router.post("/{student_id}/reset_password")
def reset_password(student_id: str, request: Request):
    sess = _current_admin(request)
    with db.connection() as conn:
        acct = conn.execute(
            "SELECT * FROM account WHERE student_id = ?", (student_id,)
        ).fetchone()
        if acct is None:
            raise HTTPException(status_code=404, detail="未找到账号")
        salt = auth.new_salt()
        client_hash = auth.compute_client_hash(student_id, salt)
        conn.execute(
            "UPDATE account SET pw_salt = ?, pw_hash = ?, pw_changed = 0, updated_at = ?"
            " WHERE student_id = ?",
            (salt, auth.store_hash(client_hash), auth._utcnow().isoformat(), student_id),
        )
        # 重置即踢掉该账号的全部会话（重置通常因密码泄露/遗忘，旧会话一律作废）
        conn.execute("DELETE FROM login_session WHERE student_id = ?", (student_id,))
        # 一并解除登录限流锁定：重置多发生在用户已多次试错被锁之后，
        # 锁定按学号计，不随密码重置自动失效，得由管理员操作顺带清掉
        auth.clear_login_failures(conn, student_id)
        auth.write_audit(conn, sess["student_id"], "reset_password", student_id,
                         "重置为初始密码并解除登录锁定")
    return {"ok": True}
