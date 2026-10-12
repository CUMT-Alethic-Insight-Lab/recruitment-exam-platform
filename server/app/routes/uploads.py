import hmac
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import cleaned_report, config, db
from ..auth import require_admin
from ..constants import MAX_ATTACHMENT_BYTES  # noqa: F401  兼容既有 import 路径
from ..schemas import ExamSessionOut
from .campaigns import get_campaign_or_404, now_iso
from .exam import _row_to_out, get_session_or_404, save_zip_attachment

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
    slot: int = Form(-1),
    cleaned: UploadFile = File(...),
    attachment: UploadFile | None = File(None),
    x_session_token: str | None = Header(default=None),
):
    """交卷。slot >= 0 时为逐题提交：同一考试会话按题各存一份清洗件
    （cleaned_q{N}.jsonl，登记进行 slots JSON），每题独立幂等；
    slot < 0 为旧的整卷单件管线，保持不变。"""
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        row = get_session_or_404(conn, session_id)
        if row["campaign_id"] != cid:
            raise HTTPException(status_code=404, detail="会话不属于该批次")
        if not x_session_token or not hmac.compare_digest(
                row["session_token"], x_session_token):
            raise HTTPException(status_code=401, detail="会话凭证无效")
        slot_key = str(slot)
        try:
            slots = json.loads(row["slots"] or "{}")
        except ValueError:
            slots = {}
        if slot >= 0:
            if row["status"] == "voided":
                raise HTTPException(status_code=409, detail="会话已作废，无法交卷")
            if slot_key in slots:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "already_uploaded",
                            "message": "该题已交卷，请勿重复提交"},
                )
        else:
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

        attachment_path = row["attachment_file_path"]
        if attachment is not None and attachment.filename:
            attachment_path = save_zip_attachment(attachment, cid, session_id)

        target_dir = _uploads_dir(cid, session_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        ext = ".md" if cleaned_name.endswith(".md") else ".jsonl"
        uploaded_at = now_iso()
        if slot >= 0:
            cleaned_path = target_dir / f"cleaned_q{slot}{ext}"
            with cleaned_path.open("wb") as f:
                shutil.copyfileobj(cleaned.file, f)
            slots[slot_key] = {
                "path": str(cleaned_path),
                "name": cleaned_name,
                "uploaded_at": uploaded_at,
            }
            conn.execute(
                "UPDATE exam_session SET status = 'uploaded',"
                " uploaded_at = COALESCE(uploaded_at, ?),"
                " slots = ?, repo_url = COALESCE(?, repo_url),"
                " attachment_file_path = ? WHERE id = ?",
                (uploaded_at, json.dumps(slots, ensure_ascii=False),
                 repo_url or None, attachment_path, session_id),
            )
        else:
            cleaned_path = target_dir / f"cleaned{ext}"
            with cleaned_path.open("wb") as f:
                shutil.copyfileobj(cleaned.file, f)
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
    cid: str, sid: str, kind: str, slot: int | None = None,
    format: str = "jsonl",
    _: None = Depends(require_admin),
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
            if slot is not None:
                try:
                    slots = json.loads(row["slots"] or "{}")
                except ValueError:
                    slots = {}
                entry = slots.get(str(slot))
                path = entry.get("path") if entry else None
            media_type = "application/octet-stream"
        else:
            path = row["attachment_file_path"]
            media_type = "application/zip"
        if not path or not Path(path).is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        if kind == "cleaned" and format != "jsonl":
            if format != "md":
                raise HTTPException(status_code=400, detail="无效格式（jsonl/md）")
            suffix = "" if slot is None else f"_q{slot}"
            header = [
                ("学号", row["student_id"]),
                ("轨迹", row["track"]),
                ("开考时间", row["opened_at"]),
                ("交卷时间", row["uploaded_at"] or ""),
            ]
            if slot is not None:
                header.append(("题号", f"第 {slot + 1} 题"))
            md = cleaned_report.records_to_markdown(
                cleaned_report.load_records(path), header)
            return Response(
                content=md.encode("utf-8"),
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition":
                         f'attachment; filename="cleaned{suffix}.md"'},
            )
        return FileResponse(path, media_type=media_type, filename=Path(path).name)
