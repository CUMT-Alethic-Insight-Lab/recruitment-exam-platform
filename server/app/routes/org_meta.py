from fastapi import APIRouter, Request

from .. import db
from ..org_auth import require_org_identity

router = APIRouter(prefix="/api/v1", tags=["organization identity"])


@router.get("/me")
def me(request: Request):
    identity = require_org_identity(request, scope="profile:read:self")
    with db.connection() as conn:
        account = conn.execute(
            "SELECT nickname FROM account WHERE student_id = ?", (identity.student_id,)
        ).fetchone()
    display_name = account["nickname"] if account and account["nickname"] else "成员"
    return {
        "id": identity.principal_id,
        "principal_id": identity.principal_id,
        "display_name": display_name,
        "member_status": "active",
        "role": identity.role,
        "auth_type": identity.auth_type,
        "scopes": sorted(identity.scopes),
    }


@router.get("/capabilities")
def capabilities(request: Request):
    identity = require_org_identity(request)
    values = sorted(identity.scopes)
    return {
        "capabilities": values,
        "scopes": values,
        "auth_type": identity.auth_type,
        "confirmation": "trusted_web" if identity.auth_type == "web" else "trusted_web_required",
    }
