import hashlib
import json
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import auth, config, db

READ_SCOPES = frozenset({
    "profile:read:self", "knowledge:read", "projects:read", "applications:read:self",
})
CLI_SCOPES = READ_SCOPES | {"applications:create"}
WEB_SCOPES = CLI_SCOPES


@dataclass(frozen=True)
class OrgIdentity:
    student_id: str
    role: str
    principal_id: str
    auth_type: str
    scopes: frozenset[str]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def credential_hash(account) -> str:
    return digest(account["pw_hash"] or "")


def principal_for(conn, student_id: str) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO org_principal (id, student_id, created_at) VALUES (?, ?, ?)",
        ("prn_" + secrets.token_urlsafe(24), student_id, auth._utcnow().isoformat()),
    )
    return conn.execute(
        "SELECT id FROM org_principal WHERE student_id = ?", (student_id,)
    ).fetchone()["id"]


def revoke_cli(conn, student_id: str) -> None:
    now = auth._utcnow().isoformat()
    conn.execute(
        "UPDATE org_cli_grant SET revoked_at = ? WHERE principal_id IN"
        " (SELECT id FROM org_principal WHERE student_id = ?) AND revoked_at IS NULL",
        (now, student_id),
    )
    conn.execute(
        "UPDATE org_cli_authorization SET used_at = ? WHERE principal_id IN"
        " (SELECT id FROM org_principal WHERE student_id = ?) AND used_at IS NULL",
        (now, student_id),
    )


def require_org_identity(request: Request, scope: str | None = None,
                         web_only: bool = False) -> OrgIdentity:
    token = auth.bearer_token(request)
    if not token:
        raise auth.unauthorized()
    with db.connection() as conn:
        session = auth.lookup_session(conn, token)
        if session is not None:
            account = conn.execute(
                "SELECT * FROM account WHERE student_id = ?", (session["student_id"],)
            ).fetchone()
            principal_id = None
            auth_type = "web"
            scopes = WEB_SCOPES
        else:
            grant = conn.execute(
                "SELECT g.*, p.student_id FROM org_cli_grant g"
                " JOIN org_principal p ON p.id = g.principal_id WHERE token_hash = ?",
                (digest(token),),
            ).fetchone()
            if grant is None or grant["revoked_at"] or grant["expires_at"] <= auth._utcnow().isoformat():
                raise auth.unauthorized()
            account = conn.execute(
                "SELECT * FROM account WHERE student_id = ?", (grant["student_id"],)
            ).fetchone()
            if account is None or grant["credential_hash"] != credential_hash(account):
                raise auth.unauthorized()
            principal_id = grant["principal_id"]
            auth_type = "cli"
            scopes = frozenset(json.loads(grant["scopes_json"])) & CLI_SCOPES
        if account is None or account["role"] not in ("member", "admin"):
            raise HTTPException(status_code=403, detail="需要当前组织成员身份")
        if not account["pw_changed"]:
            raise auth.force_password_change()
        if web_only and auth_type != "web":
            raise HTTPException(status_code=403, detail="此操作仅允许网页登录会话")
        if scope is not None and scope not in scopes:
            raise HTTPException(status_code=403, detail="客户端授权范围不足")
        if config.IS_EDGE and request.method not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(
                status_code=503,
                detail="组织写操作仅由 authoritative 实例处理",
                headers={"Retry-After": "60"},
            )
        if principal_id is None:
            principal_id = conn.execute(
                "SELECT id FROM org_principal WHERE student_id = ?", (account["student_id"],)
            ).fetchone()
            if principal_id is not None:
                principal_id = principal_id["id"]
            elif config.IS_EDGE:
                raise HTTPException(status_code=503, detail="edge 缺少权威主体映射，请回源 authoritative",
                                    headers={"Retry-After": "60"})
            else:
                principal_id = principal_for(conn, account["student_id"])
        return OrgIdentity(account["student_id"], account["role"], principal_id,
                           auth_type, frozenset(scopes))
