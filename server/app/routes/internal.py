"""内部接口（仅 authoritative/lab 挂载，见 main.py）。

POST /api/internal/ingest-staged：云端 edge 暂存件的收口端点（M21）。
Header X-Staging-Secret 共享密钥鉴权；载荷 = staging_forward 转发的
meta（manifest 字段）+ 全部暂存文件（表单名 file:{相对路径}）。

每个 slot 与正常交卷走完全一致的落库收敛点（submission.commit_uploaded）：
文件归位 data/uploads/{cid}/{sid}/ → session uploaded → application tested
→ 台账登记 source=staged；同 (session, slot, sha256) 命中台账直接幂等 ack。
任何一件校验失败即整体报错，云端保留暂存待下轮重发。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.datastructures import UploadFile as StarletteUploadFile

from .. import config, db, submission
from .campaigns import get_campaign_or_404

router = APIRouter(prefix="/api/internal", tags=["internal"])


def _check_secret(header: str) -> None:
    if not config.STAGING_SECRET or not hmac.compare_digest(
            config.STAGING_SECRET, header):
        raise HTTPException(status_code=403, detail="暂存收口密钥无效")


def _load_file(staged_files: dict, meta: dict) -> tuple[bytes, str]:
    up = staged_files.get(meta["path"])
    if up is None:
        raise HTTPException(status_code=400,
                            detail=f"暂存文件缺失: {meta['path']}")
    data = up.file.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != meta.get("sha256"):
        raise HTTPException(status_code=400,
                            detail=f"文件 sha256 校验不符: {meta['path']}")
    return data, digest


@router.post("/ingest-staged")
async def ingest_staged(request: Request,
                        x_staging_secret: str = Header(default="")):
    _check_secret(x_staging_secret)
    form = await request.form()
    cid = str(form.get("campaign_id") or "")
    sid = str(form.get("session_id") or "")
    repo_url = str(form.get("repo_url") or "")
    try:
        manifest = json.loads(str(form.get("manifest") or ""))
        uploads = manifest["uploads"]
        assert isinstance(uploads, dict) and uploads
    except (ValueError, KeyError, AssertionError):
        raise HTTPException(status_code=400, detail="manifest 无效") from None
    staged_files = {
        k[5:]: v for k, v in form.items()
        if k.startswith("file:") and isinstance(v, StarletteUploadFile)
    }
    try:
        slot_keys = sorted(uploads, key=lambda k: int(k))
    except ValueError:
        raise HTTPException(status_code=400, detail="slot 非法") from None
    results = []
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        row = conn.execute(
            "SELECT * FROM exam_session WHERE id = ?", (sid,)).fetchone()
        if row is None or row["campaign_id"] != cid:
            raise HTTPException(status_code=404, detail="考试会话不存在")
        for slot_key in slot_keys:
            entry = uploads[slot_key]
            slot = int(slot_key)
            files_meta = (entry or {}).get("files") or []
            cleaned_meta = next(
                (f for f in files_meta if f.get("field") == "cleaned"), None)
            if cleaned_meta is None:
                raise HTTPException(status_code=400,
                                    detail=f"slot {slot_key} 缺清洗件")
            data, digest = _load_file(staged_files, cleaned_meta)
            # 台账幂等：同 session+slot+hash 直接 ack（重发/直连竞态）
            if submission.ledger_seen(sid, slot, digest):
                results.append({"slot": slot, "status": "deduped"})
                continue
            # 状态校验与正常交卷同口径
            try:
                slots = json.loads(row["slots"] or "{}")
            except ValueError:
                slots = {}
            if row["status"] == "voided":
                raise HTTPException(status_code=409, detail="会话已作废，无法交卷")
            if slot >= 0:
                if slot_key in slots:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "already_uploaded",
                                "message": "该题已交卷且内容不同，请人工核查"},
                    )
            elif row["status"] != "draft":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "already_uploaded",
                            "message": "该会话已交卷且内容不同，请人工核查"},
                )
            # 文件归位：与正常交卷同一目录与命名
            target_dir = Path(config.DATA_DIR) / "uploads" / cid / sid
            target_dir.mkdir(parents=True, exist_ok=True)
            cleaned_name = Path(cleaned_meta.get("name") or "cleaned.jsonl").name
            ext = ".md" if cleaned_name.endswith(".md") else ".jsonl"
            cleaned_path = target_dir / (f"cleaned_q{slot}{ext}" if slot >= 0
                                         else f"cleaned{ext}")
            cleaned_path.write_bytes(data)
            attachment_path = row["attachment_file_path"]
            file_hashes = [("cleaned", digest)]
            att_meta = next(
                (f for f in files_meta if f.get("field") == "attachment"), None)
            if att_meta is not None:
                adata, adigest = _load_file(staged_files, att_meta)
                aname = Path(att_meta.get("name") or "attachment.zip").name
                apath = target_dir / ("attachment"
                                      + (Path(aname).suffix or ".zip"))
                apath.write_bytes(adata)
                attachment_path = str(apath)
                file_hashes.append(("attachment", adigest))
            row, is_new = submission.commit_uploaded(
                conn, row, slot=slot, cleaned_path=cleaned_path,
                cleaned_name=cleaned_name, repo_url=repo_url or None,
                attachment_path=attachment_path, file_hashes=file_hashes,
                source="staged")
            results.append({"slot": slot,
                            "status": "ingested" if is_new else "deduped"})
    return {"status": "ok", "session_id": sid, "results": results}
