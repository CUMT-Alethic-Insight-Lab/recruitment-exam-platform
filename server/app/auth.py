import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import config, db

PBKDF2_ITERATIONS = 100_000
PBKDF2_DKLEN = 32

_PEPPER_INFO = b"pw-pepper-v1"


def pepper() -> bytes:
    return hmac.new(config.SECRET_KEY.encode(), _PEPPER_INFO, hashlib.sha256).digest()


def new_salt() -> str:
    return secrets.token_hex(16)


def compute_client_hash(password: str, salt: str) -> str:
    """服务端代算 PBKDF2 client_hash（仅注册默认密码与管理员播种时使用）。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS, PBKDF2_DKLEN
    ).hex()


def store_hash(client_hash: str) -> str:
    return hmac.new(pepper(), client_hash.encode(), hashlib.sha256).hexdigest()


def verify_client_hash(stored: str | None, client_hash: str) -> bool:
    if not stored:
        return False
    return hmac.compare_digest(stored, store_hash(client_hash))


def bearer_token(request: Request) -> str:
    return request.headers.get("Authorization", "").removeprefix("Bearer ").strip()


def unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="请重新登录")


def force_password_change() -> HTTPException:
    return HTTPException(status_code=403, detail="请先修改初始密码")


def pseudo_salt(student_id: str) -> str:
    """不存在的学号返回确定性伪盐（防账号枚举），格式与真盐一致（32 位 hex）。"""
    return hmac.new(config.SECRET_KEY.encode(),
                    f"pseudo-salt:{student_id}".encode(), hashlib.sha256).hexdigest()[:32]


# 常见弱密码字典：改密时用新盐现场算 client_hash 比对拒绝（服务端只见散列的低成本兜底）
WEAK_PASSWORDS = (
    "123456", "12345678", "123456789", "1234567890", "111111", "000000",
    "666666", "888888", "123123", "123321", "1234567", "11111111",
    "qwerty", "password", "abc123", "admin123", "iloveyou", "qwe123",
    "a123456", "root123",
)

def client_ip(request: Request) -> str:
    """限流键用的真实客户端 IP。

    lab 部署上公网流量全部经云端 nginx 转发，uvicorn 的对端恒为云端服务器，
    直接按对端 IP 限流会把所有用户挤进同一额度；故优先取代理链最左的
    X-Forwarded-For（云端 nginx 已按 $proxy_add_x_forwarded_for 正确续链）。
    注意：这两个头只在反代之后可信；直连未受保护端口时可伪造，
    伪造后果仅为限流键漂移，不构成越权。
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    return request.client.host if request.client else "unknown"


LOGIN_FAIL_WINDOW_MIN = 10
LOGIN_FAIL_LIMIT = 10
LOGIN_LOCK_MIN = 10


def check_login_rate(conn: sqlite3.Connection, key: str) -> None:
    """学号维度的登录限流：锁定中一律 429。"""
    row = conn.execute(
        "SELECT locked_until FROM login_attempt WHERE key = ?", (key,)
    ).fetchone()
    if row is not None and row["locked_until"] \
            and row["locked_until"] > _utcnow().isoformat():
        raise HTTPException(status_code=429, detail="尝试次数过多，请 10 分钟后再试")


def record_login_failure(key: str) -> None:
    # 独立连接落库：登录失败路径随即抛 401，调用方连接的未提交写入会被回滚
    now = _utcnow()
    with db.connection() as conn:
        row = conn.execute(
            "SELECT fails, window_start FROM login_attempt WHERE key = ?", (key,)
        ).fetchone()
        if row is not None and row["window_start"] and datetime.fromisoformat(
                row["window_start"]) < now - timedelta(minutes=LOGIN_FAIL_WINDOW_MIN):
            row = None  # 计数窗口已过期，重新累计
        fails = (row["fails"] if row is not None else 0) + 1
        window_start = row["window_start"] if row is not None else now.isoformat()
        locked_until = (now + timedelta(minutes=LOGIN_LOCK_MIN)).isoformat() \
            if fails >= LOGIN_FAIL_LIMIT else None
        conn.execute(
            "INSERT INTO login_attempt (key, fails, window_start, locked_until)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET fails = excluded.fails,"
            " window_start = excluded.window_start, locked_until = excluded.locked_until",
            (key, fails, window_start, locked_until),
        )


def clear_login_failures(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM login_attempt WHERE key = ?", (key,))


def write_audit(conn: sqlite3.Connection, actor: str, action: str,
                target: str = "", detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (actor, action, target, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (actor, action, target, detail, _utcnow().isoformat()),
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def issue_session(conn: sqlite3.Connection, kind: str, campaign_id: str | None,
                  student_id: str | None) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = _utcnow()
    conn.execute(
        "INSERT INTO login_session (token_hash, kind, campaign_id, student_id,"
        " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token_hash, kind, campaign_id, student_id, now.isoformat(),
         (now + timedelta(days=config.SESSION_DAYS)).isoformat()),
    )
    return token


def lookup_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = conn.execute(
        "SELECT * FROM login_session WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _utcnow().isoformat():
        conn.execute("DELETE FROM login_session WHERE token_hash = ?", (token_hash,))
        return None
    acct = conn.execute(
        "SELECT role FROM account WHERE student_id = ?", (row["student_id"],)
    ).fetchone()
    if acct is None or acct["role"] != row["kind"]:
        return None
    return row


def require_admin_session(request: Request) -> sqlite3.Row:
    """管理员会话门禁：pw_changed=0（初始密码）的管理账号改密前一律 403。"""
    token = bearer_token(request)
    if not token:
        raise unauthorized()
    with db.connection() as conn:
        row = lookup_session(conn, token)
        if row is None or row["kind"] != "admin":
            raise unauthorized()
        acct = conn.execute(
            "SELECT pw_changed, role FROM account WHERE student_id = ?",
            (row["student_id"],),
        ).fetchone()
    if acct is None or acct["role"] != "admin":
        raise unauthorized()
    if not acct["pw_changed"]:
        raise force_password_change()
    return row


def require_admin(request: Request) -> None:
    require_admin_session(request)


def require_applicant(request: Request, cid: str) -> str:
    # 机试为线下监考场景，考生在考试机上可能仍是初始密码：本门禁不卡 pw_changed，
    # 强制改密只作用于 /api/auth/* 个人资料类接口与管理接口（require_admin_session）
    token = bearer_token(request)
    if not token:
        raise unauthorized()
    with db.connection() as conn:
        row = lookup_session(conn, token)
    if row is None or row["kind"] != "applicant":
        raise unauthorized()
    if row["campaign_id"] != cid:
        raise HTTPException(status_code=403, detail="登录会话不属于该批次")
    return row["student_id"]
