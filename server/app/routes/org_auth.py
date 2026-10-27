import hmac
import json
import secrets
import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .. import auth, config, db
from ..org_auth import CLI_SCOPES, READ_SCOPES, credential_hash, digest, require_org_identity

router = APIRouter(prefix="/api/v1/auth/cli", tags=["org-auth"])


class AuthorizationBody(BaseModel):
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    scopes: list[str] = Field(default_factory=lambda: sorted(READ_SCOPES), max_length=16)


class TokenBody(BaseModel):
    request_id: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=1, max_length=128)


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/authorization-requests")
def authorize(body: AuthorizationBody, request: Request, response: Response):
    identity = require_org_identity(request, web_only=True)
    scopes = frozenset(body.scopes)
    if not scopes or not scopes <= CLI_SCOPES:
        raise HTTPException(status_code=400, detail="无效或不允许的 CLI scope")
    now = auth._utcnow()
    expires_at = (now + timedelta(seconds=config.ORG_CLI_CODE_SECONDS)).isoformat()
    code = secrets.token_urlsafe(24)
    with db.connection() as conn:
        account = conn.execute("SELECT * FROM account WHERE student_id = ?",
                               (identity.student_id,)).fetchone()
        recent = conn.execute(
            "SELECT COUNT(*) FROM org_cli_authorization WHERE principal_id = ? AND created_at > ?",
            (identity.principal_id, (now - timedelta(minutes=10)).isoformat()),
        ).fetchone()[0]
        if recent >= 10:
            raise HTTPException(status_code=429, detail="授权请求过多", headers={"Retry-After": "600"})
        try:
            conn.execute(
                "INSERT INTO org_cli_authorization"
                " (request_id, principal_id, code_hash, scopes_json, credential_hash, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (body.request_id, identity.principal_id, digest(code), json.dumps(sorted(scopes)),
                 credential_hash(account), now.isoformat(), expires_at),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="授权请求标识已使用")
        auth.write_audit(conn, identity.principal_id, "cli_authorize", body.request_id)
    private_response(response)
    return {"code": code, "expires_at": expires_at}


@router.post("/token")
def exchange(body: TokenBody, response: Response):
    if config.IS_EDGE:
        raise HTTPException(status_code=503, detail="CLI 授权仅由 authoritative 实例处理",
                            headers={"Retry-After": "60"})
    result = None
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM org_cli_authorization WHERE request_id = ?",
                           (body.request_id,)).fetchone()
        now = auth._utcnow()
        if (row is not None and row["used_at"] is None
                and row["expires_at"] > now.isoformat()
                and row["attempts"] < config.ORG_CLI_CODE_ATTEMPTS):
            conn.execute("UPDATE org_cli_authorization SET attempts = attempts + 1 WHERE request_id = ?",
                         (body.request_id,))
            account = conn.execute(
                "SELECT a.* FROM account a JOIN org_principal p ON p.student_id = a.student_id"
                " WHERE p.id = ?", (row["principal_id"],),
            ).fetchone()
            if (hmac.compare_digest(row["code_hash"], digest(body.code))
                    and account is not None and account["role"] in ("member", "admin")
                    and account["pw_changed"]
                    and row["credential_hash"] == credential_hash(account)):
                token = "org_cli_" + secrets.token_urlsafe(32)
                grant_id = "grt_" + secrets.token_urlsafe(24)
                expires_at = (now + timedelta(seconds=config.ORG_CLI_TOKEN_SECONDS)).isoformat()
                conn.execute(
                    "INSERT INTO org_cli_grant"
                    " (id, principal_id, token_hash, scopes_json, credential_hash, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (grant_id, row["principal_id"], digest(token), row["scopes_json"],
                     row["credential_hash"], now.isoformat(), expires_at),
                )
                conn.execute("UPDATE org_cli_authorization SET used_at = ? WHERE request_id = ?",
                             (now.isoformat(), body.request_id))
                auth.write_audit(conn, row["principal_id"], "cli_exchange", grant_id)
                result = {"token": token, "expires_at": expires_at,
                          "scopes": json.loads(row["scopes_json"])}
    if result is None:
        raise HTTPException(status_code=401, detail="授权码无效或已过期")
    private_response(response)
    return result


@router.get("/grants")
def grants(request: Request, response: Response,
           limit: int = Query(default=50, ge=1, le=100),
           cursor: str | None = Query(default=None, max_length=200)):
    identity = require_org_identity(request, web_only=True)
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, scopes_json, created_at, expires_at, revoked_at FROM org_cli_grant"
            " WHERE principal_id = ? AND id > ? ORDER BY id LIMIT ?",
            (identity.principal_id, cursor or "", limit + 1),
        ).fetchall()
    private_response(response)
    return {"items": [{"id": r["id"], "scopes": json.loads(r["scopes_json"]),
                       "created_at": r["created_at"], "expires_at": r["expires_at"],
                       "revoked_at": r["revoked_at"]} for r in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}


@router.delete("/grants/{grant_id}")
def revoke(grant_id: str, request: Request):
    identity = require_org_identity(request, web_only=True)
    with db.connection() as conn:
        changed = conn.execute(
            "UPDATE org_cli_grant SET revoked_at = COALESCE(revoked_at, ?)"
            " WHERE id = ? AND principal_id = ?",
            (auth._utcnow().isoformat(), grant_id, identity.principal_id),
        ).rowcount
        if not changed:
            raise HTTPException(status_code=404, detail="未找到授权")
        auth.write_audit(conn, identity.principal_id, "cli_revoke", grant_id)
    return {"ok": True}
