import hmac
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config, db
from ..auth import require_admin
from ..schemas import ExamSessionOut
from .campaigns import get_campaign_or_404, now_iso
from .exam import _row_to_out, get_session_or_404

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

router = APIRouter(prefix="/api/campaigns", tags=["uploads"])


class LanUploadedBody(BaseModel):
    """upload_mode=lan 的轻量交卷标记：文件存考场局域网接收机，不传云端。"""
    repo_url: str = ""


def _uploads_dir(cid: str, sid: str) -> Path:
    return Path(config.DATA_DIR) / "uploads" / cid / sid


@router.post("/{cid}/sessions", response_model=ExamSessionOut)
def upload_session(
    cid: str,
    session_id: str = Form(...),
    repo_url: str = Form(""),
    cleaned: UploadFile = File(...),
    attachment: UploadFile | None = File(None),
    x_session_token: str | None = Header(default=None),
):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        row = get_session_or_404(conn, session_id)
        if row["campaign_id"] != cid:
            raise HTTPException(status_code=404, detail="会话不属于该批次")
        if not x_session_token or not hmac.compare_digest(
                row["session_token"], x_session_token):
            raise HTTPException(status_code=401, detail="会话凭证无效")
        if row["status"] == "uploaded":
            raise HTTPException(
                status_code=409,
                detail={"code": "already_uploaded", "message": "该会话已交卷，请勿重复提交"},
            )
        if row["status"] != "draft":
            raise HTTPException(status_code=409, detail="会话已作废，无法交卷")

        cleaned_name = cleaned.filename or ""
        if not (cleaned_name.endswith(".jsonl") or cleaned_name.endswith(".md")):
            raise HTTPException(status_code=400, detail="清洗件须为 .jsonl 或 .md 文件")
        if repo_url and not repo_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="repo_url 须以 http(s):// 开头")

        attachment_path = None
        if attachment is not None and attachment.filename:
            if not attachment.filename.endswith(".zip"):
                raise HTTPException(status_code=400, detail="附件须为 .zip 文件")
            target_dir = _uploads_dir(cid, session_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            att_path = target_dir / "attachment.zip"
            too_large = False
            size = 0
            with att_path.open("wb") as f:
                while chunk := attachment.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ATTACHMENT_BYTES:
                        too_large = True
                        break
                    f.write(chunk)
            if too_large:
                att_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="附件超过 50MB 上限")
            attachment_path = str(att_path)

        target_dir = _uploads_dir(cid, session_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        ext = ".md" if cleaned_name.endswith(".md") else ".jsonl"
        cleaned_path = target_dir / f"cleaned{ext}"
        with cleaned_path.open("wb") as f:
            shutil.copyfileobj(cleaned.file, f)

        uploaded_at = now_iso()
        conn.execute(
            "UPDATE exam_session SET status = 'uploaded', uploaded_at = ?,"
            " cleaned_file_path = ?, repo_url = ?, attachment_file_path = ? WHERE id = ?",
            (uploaded_at, str(cleaned_path), repo_url or None,
             attachment_path, session_id),
        )
        conn.execute(
            "UPDATE application SET status = 'tested', updated_at = ?"
            " WHERE campaign_id = ? AND student_id = ? AND status = 'applied'",
            (uploaded_at, cid, row["student_id"]),
        )
        updated = get_session_or_404(conn, session_id)
        return _row_to_out(updated)


@router.post("/{cid}/sessions/{sid}/lan-uploaded", response_model=ExamSessionOut)
def mark_lan_uploaded(
    cid: str,
    sid: str,
    body: LanUploadedBody | None = None,
    x_session_token: str | None = Header(default=None),
):
    """局域网交卷标记：轨迹文件存考场接收机（session_receiver.py），云端只记状态。

    与 upload_session 同口径校验会话凭证与状态；不落任何文件，
    cleaned/attachment 路径置空，评审页据 stored_lan 提示从接收机调取。"""
    body = body or LanUploadedBody()
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        row = get_session_or_404(conn, sid)
        if row["campaign_id"] != cid:
            raise HTTPException(status_code=404, detail="会话不属于该批次")
        if not x_session_token or not hmac.compare_digest(
                row["session_token"], x_session_token):
            raise HTTPException(status_code=401, detail="会话凭证无效")
        if row["status"] == "uploaded":
            raise HTTPException(
                status_code=409,
                detail={"code": "already_uploaded", "message": "该会话已交卷，请勿重复提交"},
            )
        if row["status"] != "draft":
            raise HTTPException(status_code=409, detail="会话已作废，无法交卷")
        if body.repo_url and not body.repo_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="repo_url 须以 http(s):// 开头")
        uploaded_at = now_iso()
        conn.execute(
            "UPDATE exam_session SET status = 'uploaded', uploaded_at = ?,"
            " cleaned_file_path = NULL, attachment_file_path = NULL, repo_url = ?"
            " WHERE id = ?",
            (uploaded_at, body.repo_url or None, sid),
        )
        conn.execute(
            "UPDATE application SET status = 'tested', updated_at = ?"
            " WHERE campaign_id = ? AND student_id = ? AND status = 'applied'",
            (uploaded_at, cid, row["student_id"]),
        )
        updated = get_session_or_404(conn, sid)
        return _row_to_out(updated)


@router.get("/{cid}/sessions/{sid}/files/{kind}")
def download_session_file(
    cid: str, sid: str, kind: str, _: None = Depends(require_admin)
):
    if kind not in ("cleaned", "attachment"):
        raise HTTPException(status_code=404, detail="无效文件类型")
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        row = get_session_or_404(conn, sid)
        if row["campaign_id"] != cid:
            raise HTTPException(status_code=404, detail="会话不属于该批次")
        if kind == "cleaned":
            path = row["cleaned_file_path"]
            media_type = "application/octet-stream"
        else:
            path = row["attachment_file_path"]
            media_type = "application/zip"
        if not path or not Path(path).is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(path, media_type=media_type, filename=Path(path).name)
