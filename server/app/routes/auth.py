import binascii
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from .. import auth, config, db
from ..auth import require_applicant
from ..schemas import ApplicantMeOut

router = APIRouter(tags=["auth"])

AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}

# 昵称：trim 后 1–16 字符，仅中文/字母/数字/中间空格/_/-/·
_NICKNAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_·\- ]+$")


class ApplicantLoginBody(BaseModel):
    student_id: str
    client_hash: str


class ChangePasswordBody(BaseModel):
    old_client_hash: str
    new_salt: str
    new_client_hash: str


class ProfileBody(BaseModel):
    nickname: str


def _latest_application(conn, student_id: str):
    return conn.execute(
        "SELECT * FROM application WHERE student_id = ?"
        " ORDER BY created_at DESC, campaign_id DESC LIMIT 1",
        (student_id,),
    ).fetchone()


def _account_payload(conn, acct) -> dict:
    data = {
        "role": acct["role"],
        "student_id": acct["student_id"],
        "name": "",
        "nickname": acct["nickname"],
        "has_avatar": acct["avatar_ext"] is not None,
        "pw_changed": bool(acct["pw_changed"]),
    }
    if acct["role"] == "applicant":
        row = _latest_application(conn, acct["student_id"])
        data["name"] = row["name"] if row is not None else ""
        data["cid"] = row["campaign_id"] if row is not None else None
        data["registration_complete"] = bool(row["registration_complete"]) if row is not None else False
    return data


def _get_session(request: Request):
    token = auth.bearer_token(request)
    if not token:
        raise auth.unauthorized()
    with db.connection() as conn:
        sess = auth.lookup_session(conn, token)
        if sess is None:
            raise auth.unauthorized()
        acct = conn.execute(
            "SELECT * FROM account WHERE student_id = ?", (sess["student_id"],)
        ).fetchone()
    if acct is None:
        raise auth.unauthorized()
    return sess, acct


def _require_pw_changed(acct) -> None:
    # 初始密码（pw_changed=0）账号只放行 me / change_password / logout
    if not acct["pw_changed"]:
        raise auth.force_password_change()


def _require_session(request: Request):
    token = auth.bearer_token(request)
    if not token:
        raise auth.unauthorized()
    with db.connection() as conn:
        return auth.lookup_session(conn, token)


def validate_nickname(raw: str) -> str:
    value = raw.strip()
    if not (1 <= len(value) <= 16):
        raise HTTPException(status_code=400, detail="昵称须为 1-16 个字符")
    if not _NICKNAME_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail="昵称仅允许中文/字母/数字/中间空格/_/-/·，且首尾不能有空格",
        )
    return value


def avatar_dir() -> Path:
    path = Path(config.DATA_DIR) / "avatars"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _detect_image_ext(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ---------- 统一登录（account 表） ----------


@router.get("/api/auth/salt")
def auth_salt(student_id: str, cid: str | None = None):
    # cid 兼容保留（exam_kit 仍会传），忽略
    with db.connection() as conn:
        auth.check_login_rate(conn, student_id)
        row = conn.execute(
            "SELECT pw_salt FROM account WHERE student_id = ?", (student_id,)
        ).fetchone()
        if row is None or not row["pw_salt"]:
            # 防账号枚举：不区分"学号不存在"，返回确定性伪盐；伪盐请求计入限流
            auth.record_login_failure(student_id)
            return {"salt": auth.pseudo_salt(student_id)}
        return {"salt": row["pw_salt"]}


@router.post("/api/auth/login")
def unified_login(body: ApplicantLoginBody):
    with db.connection() as conn:
        auth.check_login_rate(conn, body.student_id)
        acct = conn.execute(
            "SELECT * FROM account WHERE student_id = ?", (body.student_id,)
        ).fetchone()
        # 学号不存在与密码错误统一 401（防账号枚举）
        if acct is None or not auth.verify_client_hash(acct["pw_hash"], body.client_hash):
            auth.record_login_failure(body.student_id)
            raise HTTPException(status_code=401, detail="学号或密码错误")
        auth.clear_login_failures(conn, body.student_id)
        cid = None
        if acct["role"] == "applicant":
            row = _latest_application(conn, acct["student_id"])
            cid = row["campaign_id"] if row is not None else None
        token = auth.issue_session(conn, acct["role"], cid, acct["student_id"])
        return {"token": token, **_account_payload(conn, acct)}


@router.get("/api/auth/me")
def auth_me(request: Request):
    _, acct = _get_session(request)
    with db.connection() as conn:
        return _account_payload(conn, acct)


@router.post("/api/auth/profile")
def update_profile(body: ProfileBody, request: Request):
    _, acct = _get_session(request)
    _require_pw_changed(acct)
    nickname = validate_nickname(body.nickname)
    with db.connection() as conn:
        conn.execute(
            "UPDATE account SET nickname = ?, updated_at = ? WHERE student_id = ?",
            (nickname, auth._utcnow().isoformat(), acct["student_id"]),
        )
    return {"ok": True, "nickname": nickname}


@router.post("/api/auth/avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    _, acct = _get_session(request)
    _require_pw_changed(acct)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="头像文件为空")
    if len(data) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="头像不能超过 2MB")
    ext = _detect_image_ext(data)
    if ext is None:
        raise HTTPException(status_code=400, detail="仅支持 PNG/JPEG/WebP 图片")
    directory = avatar_dir()
    (directory / f"{acct['student_id']}.{ext}").write_bytes(data)
    old_ext = acct["avatar_ext"]
    if old_ext and old_ext != ext:
        (directory / f"{acct['student_id']}.{old_ext}").unlink(missing_ok=True)
    with db.connection() as conn:
        conn.execute(
            "UPDATE account SET avatar_ext = ?, updated_at = ? WHERE student_id = ?",
            (ext, auth._utcnow().isoformat(), acct["student_id"]),
        )
    return {"ok": True, "has_avatar": True}


@router.get("/api/auth/avatar")
def get_avatar(student_id: str, request: Request):
    # 头像登录可见（不限本人）：堵住匿名按学号遍历
    if _require_session(request) is None:
        raise auth.unauthorized()
    with db.connection() as conn:
        row = conn.execute(
            "SELECT avatar_ext FROM account WHERE student_id = ?", (student_id,)
        ).fetchone()
    ext = row["avatar_ext"] if row is not None else None
    if not ext:
        raise HTTPException(status_code=404, detail="未找到头像")
    path = avatar_dir() / f"{student_id}.{ext}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="未找到头像")
    return Response(
        content=path.read_bytes(),
        media_type=AVATAR_CONTENT_TYPES[ext],
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/api/auth/change_password")
def change_password(body: ChangePasswordBody, request: Request):
    try:
        bytes.fromhex(body.new_salt)
        int(body.new_client_hash, 16)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="无效的盐值或散列格式")
    if len(body.new_salt) < 32:
        raise HTTPException(status_code=400, detail="无效的盐值或散列格式")
    if len(body.new_client_hash) != 64:
        raise HTTPException(status_code=400, detail="新散列须为 64 位 hex（32 字节）")
    sess, acct = _get_session(request)
    with db.connection() as conn:
        if not auth.verify_client_hash(acct["pw_hash"], body.old_client_hash):
            raise HTTPException(status_code=401, detail="原密码错误")
        if body.new_client_hash == auth.compute_client_hash(acct["student_id"], body.new_salt):
            raise HTTPException(status_code=400, detail="新密码不能与学号相同")
        if body.new_client_hash == body.old_client_hash:
            raise HTTPException(status_code=400, detail="新密码不能与原密码相同")
        # 服务端只见散列，弱密码只能预计算比对兜底
        if any(body.new_client_hash == auth.compute_client_hash(weak, body.new_salt)
               for weak in auth.WEAK_PASSWORDS):
            raise HTTPException(status_code=400, detail="新密码过于常见，请换更复杂的密码")
        conn.execute(
            "UPDATE account SET pw_salt = ?, pw_hash = ?, pw_changed = 1, updated_at = ?"
            " WHERE student_id = ?",
            (body.new_salt, auth.store_hash(body.new_client_hash),
             auth._utcnow().isoformat(), acct["student_id"]),
        )
        # 改密即踢掉该账号的其他会话（保留当前会话，避免前端改密成功后被弹回登录页）
        conn.execute(
            "DELETE FROM login_session WHERE student_id = ? AND token_hash != ?",
            (acct["student_id"], sess["token_hash"]),
        )
    return {"ok": True}


# ---------- 兼容保留（exam_kit 与存量页面依赖，内部改读 account 表） ----------
# 说明：旧管理端独立端点 /api/admin/salt + /api/admin/login 已移除，
# 管理端页面与统一登录入口共用 /api/auth/*；exam_kit 走下方 campaign 级兼容端点。


@router.post("/api/campaigns/{cid}/auth/login")
def applicant_login(cid: str, body: ApplicantLoginBody):
    with db.connection() as conn:
        auth.check_login_rate(conn, body.student_id)
        acct = conn.execute(
            "SELECT * FROM account WHERE student_id = ?", (body.student_id,)
        ).fetchone()
        if acct is None or not auth.verify_client_hash(acct["pw_hash"], body.client_hash):
            auth.record_login_failure(body.student_id)
            raise HTTPException(status_code=401, detail="学号或密码错误")
        auth.clear_login_failures(conn, body.student_id)
        app_row = conn.execute(
            "SELECT name FROM application WHERE campaign_id = ? AND student_id = ?",
            (cid, body.student_id),
        ).fetchone()
        token = auth.issue_session(conn, "applicant", cid, body.student_id)
        return {
            "token": token,
            "student_id": body.student_id,
            "name": app_row["name"] if app_row is not None else "",
            "pw_changed": bool(acct["pw_changed"]),
        }


@router.get("/api/campaigns/{cid}/auth/me", response_model=ApplicantMeOut)
def applicant_me(cid: str, student_id: str = Depends(require_applicant)):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM application WHERE campaign_id = ? AND student_id = ?",
            (cid, student_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="未找到报名记录")
        latest = conn.execute(
            "SELECT track FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " AND status != 'voided' ORDER BY opened_at DESC LIMIT 1",
            (cid, student_id),
        ).fetchone()
        return ApplicantMeOut(
            student_id=student_id,
            name=row["name"],
            status=row["status"],
            exam_mode=row["exam_mode"],
            registration_complete=row["registration_complete"],
            has_session=latest is not None,
            track=latest["track"] if latest is not None else None,
        )


@router.post("/api/auth/logout")
def logout(request: Request):
    token = auth.bearer_token(request)
    if not token:
        raise auth.unauthorized()
    with db.connection() as conn:
        sess = auth.lookup_session(conn, token)
        if sess is not None:
            conn.execute("DELETE FROM login_session WHERE token_hash = ?",
                         (sess["token_hash"],))
    if sess is None:
        raise auth.unauthorized()
    return {"ok": True}
