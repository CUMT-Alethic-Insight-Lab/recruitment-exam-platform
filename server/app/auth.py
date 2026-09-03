import hmac
import secrets

from fastapi import HTTPException, Request

from . import config


def verify_admin_token(token: str) -> bool:
    expected = config.ADMIN_TOKEN.encode()
    provided = token.encode()
    return bool(expected and hmac.compare_digest(expected, provided))


def require_admin(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if not token or not verify_admin_token(token):
        raise HTTPException(status_code=401, detail="管理认证失败，请重新登录")


def verify_ping(token: str) -> bool:
    return verify_admin_token(token)


def new_admin_session_token() -> str:
    return secrets.token_urlsafe(32)
