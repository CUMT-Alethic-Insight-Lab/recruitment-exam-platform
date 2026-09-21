"""自托管图形验证码（M16）：抬高中转/肉鸡批量刷注册与报名提交的攻击成本。

定位：不防定向 OCR，只要求每次写操作前先人眼读图拿一次性凭据。
- 4 位码，字符集去掉 0/o/1/l/i 等易混字符，比对大小写不敏感；
- 一次性：无论对错，原子 UPDATE 标记 used，同一条验证码不能试第二次；
- TTL 10 分钟；签发按 IP 限流（复用 login_attempt 表，键前缀隔离）。
"""

import io
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from captcha.image import ImageCaptcha
from fastapi import HTTPException

from . import config, db

CODE_LENGTH = 4
# 去掉易混字符：0/o、1/l/i；比对时统一小写
CODE_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
TTL_MINUTES = 10
ISSUE_LIMIT_PER_HOUR = 30
_RATE_KEY_PREFIX = "captcha-issue:"

_IMAGE_WIDTH = 160
_IMAGE_HEIGHT = 56


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_captcha() -> tuple[str, bytes]:
    """签发一条验证码：返回 (captcha_id, png_bytes)，明文码落库待验。"""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    buf = io.BytesIO()
    ImageCaptcha(width=_IMAGE_WIDTH, height=_IMAGE_HEIGHT).write(code, buf)
    now = _utcnow()
    captcha_id = secrets.token_urlsafe(16)
    with db.connection() as conn:
        # 顺手清理过期与已用记录，防表膨胀
        conn.execute("DELETE FROM captcha WHERE expires_at <= ? OR used = 1",
                     (now.isoformat(),))
        conn.execute(
            "INSERT INTO captcha (id, code, expires_at, used) VALUES (?, ?, ?, 0)",
            (captcha_id, code, (now + timedelta(minutes=TTL_MINUTES)).isoformat()),
        )
    return captcha_id, buf.getvalue()


def verify_captcha(captcha_id: str | None, text: str | None) -> bool:
    """一次性校验：先原子消耗记录（不存在/已用/过期一律 False），再比对码值。

    消耗与码值比对解耦——错码同样烧掉这条记录，同一条验证码最多试一次。"""
    if not captcha_id or not text:
        return False
    normalized = text.strip().lower()
    if not normalized:
        return False
    with db.connection() as conn:
        row = conn.execute(
            "UPDATE captcha SET used = 1"
            " WHERE id = ? AND used = 0 AND expires_at > ?"
            " RETURNING code",
            (captcha_id, _utcnow().isoformat()),
        ).fetchone()
        return row is not None and row["code"] == normalized


def verify_or_http(captcha_id: str | None, text: str | None) -> None:
    """接口侧校验入口：开关关闭直接放行；失败 400 + 白话消息。"""
    if not config.CAPTCHA_ENABLED:
        return
    if not verify_captcha(captcha_id, text):
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请点击图片刷新后重试")


def check_issue_rate(conn: sqlite3.Connection, ip: str) -> None:
    """签发限流：每 IP 每小时 ISSUE_LIMIT_PER_HOUR 次，超出 429。

    复用 login_attempt 表（key 前缀隔离，与登录限流互不影响）：
    fails=窗口内计数，window_start=窗口起点，locked_until 恒 NULL。
    """
    key = f"{_RATE_KEY_PREFIX}{ip}"
    now = _utcnow()
    row = conn.execute(
        "SELECT fails, window_start FROM login_attempt WHERE key = ?", (key,)
    ).fetchone()
    in_window = row is not None and row["window_start"] is not None \
        and datetime.fromisoformat(row["window_start"]) > now - timedelta(hours=1)
    count = (row["fails"] if in_window else 0) + 1
    if count > ISSUE_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="验证码获取过于频繁，请一小时后再试")
    window_start = row["window_start"] if in_window else now.isoformat()
    conn.execute(
        "INSERT INTO login_attempt (key, fails, window_start, locked_until)"
        " VALUES (?, ?, ?, NULL)"
        " ON CONFLICT(key) DO UPDATE SET fails = excluded.fails,"
        " window_start = excluded.window_start, locked_until = NULL",
        (key, count, window_start),
    )


def clear_issue_rate(conn: sqlite3.Connection, ip: str) -> None:
    conn.execute("DELETE FROM login_attempt WHERE key = ?", (f"{_RATE_KEY_PREFIX}{ip}",))
