import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from .. import config, db
from ..auth import require_admin
from ..schemas import CampaignCreate, CampaignOut, CampaignPatch
from ..constants import CAMPAIGN_STATUS

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


def _row_to_out(row: sqlite3.Row) -> CampaignOut:
    return CampaignOut(
        id=row["id"],
        name=row["name"],
        year=row["year"],
        status=row["status"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def get_campaign_or_404(conn: sqlite3.Connection, cid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="招新批次不存在")
    return row


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expires_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=config.DATA_RETENTION_DAYS)).isoformat()


@router.get("/{cid}", response_model=CampaignOut)
def get_campaign(cid: str):
    with db.connection() as conn:
        row = get_campaign_or_404(conn, cid)
        return _row_to_out(row)


@router.post("", response_model=CampaignOut, status_code=201)
def create_campaign(body: CampaignCreate, _: None = Depends(require_admin)):
    with db.connection() as conn:
        exists = conn.execute("SELECT 1 FROM campaign WHERE id = ?", (body.id,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="该批次 id 已存在")
        now = now_iso()
        conn.execute(
            "INSERT INTO campaign (id, name, year, status, expires_at, created_at)"
            " VALUES (?, ?, ?, 'open', ?, ?)",
            (body.id, body.name, body.year, expires_iso(), now),
        )
        row = conn.execute("SELECT * FROM campaign WHERE id = ?", (body.id,)).fetchone()
        return _row_to_out(row)


@router.patch("/{cid}", response_model=CampaignOut)
def patch_campaign(cid: str, body: CampaignPatch, _: None = Depends(require_admin)):
    with db.connection() as conn:
        row = get_campaign_or_404(conn, cid)
        updates = []
        params = []
        if body.name is not None:
            updates.append("name = ?")
            params.append(body.name)
        if body.status is not None:
            if body.status not in CAMPAIGN_STATUS:
                raise HTTPException(status_code=400, detail="无效状态")
            updates.append("status = ?")
            params.append(body.status)
        if body.exam_config is not None:
            updates.append("exam_config = ?")
            params.append(json.dumps(body.exam_config, ensure_ascii=False))
        if not updates:
            return _row_to_out(row)
        params.append(cid)
        conn.execute(f"UPDATE campaign SET {', '.join(updates)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
        return _row_to_out(row)


@router.get("", response_model=list[CampaignOut])
def list_campaigns(_: None = Depends(require_admin)):
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM campaign ORDER BY year DESC, created_at DESC").fetchall()
        return [_row_to_out(r) for r in rows]
