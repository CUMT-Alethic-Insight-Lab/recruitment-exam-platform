import hashlib
import hmac
import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import cleaned_report, config, db, staging, submission
from ..auth import require_admin
from ..constants import MAX_ATTACHMENT_BYTES  # noqa: F401  兼容既有 import 路径
from ..schemas import ExamSessionOut
from .campaigns import get_campaign_or_404
from .exam import _row_to_out, get_session_or_404, save_zip_attachment

router = APIRouter(prefix="/api/campaigns", tags=["uploads"])


class LanUploadedBody(BaseModel):
    """upload_mode=lan 的轻量交卷标记：文件存考场局域网接收机，不传云端。"""
    repo_url: str = ""


def _uploads_dir(cid: str, sid: str) -> Path:
    return Path(config.DATA_DIR) / "uploads" / cid / sid


def _write_with_hash(target: Path, fileobj) -> str:
    h = hashlib.sha256()
    with target.open("wb") as f:
        while chunk := fileobj.read(1024 * 1024):
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()


def _stage_on_edge(cid: str, session_id: str, slot: int, repo_url: str,
                   cleaned: UploadFile, attachment: UploadFile | None,
                   token: str | None) -> JSONResponse:
    """edge（云端只读热备）交卷：不写主库，multipart 原样暂存
    data/staging/{session_id}/，等 staging_forward 转发 lab 收口（M21）。

    本地热备库能查到会话时同口径校验凭证并带出学号；查不到（30min 同步
    窗口内新会话）也照常暂存——这正是 edge 存在的意义（隧道抖动不 503）。"""
    if not staging.SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(status_code=400, detail="session_id 非法")
    cleaned_name = cleaned.filename or ""
    if not (cleaned_name.endswith(".jsonl") or cleaned_name.endswith(".md")):
        raise HTTPException(status_code=400, detail="清洗件须为 .jsonl 或 .md 文件")
    if repo_url and not repo_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="repo_url 须以 http(s):// 开头")
    student_id = ""
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT campaign_id, student_id, session_token FROM exam_session"
                " WHERE id = ?", (session_id,)).fetchone()
    except Exception:
        row = None  # 热备库不可用/未同步不挡交卷
    if row is not None:
        if row["campaign_id"] != cid:
            raise HTTPException(status_code=404, detail="会话不属于该批次")
        if not token or not hmac.compare_digest(row["session_token"], token):
            raise HTTPException(status_code=401, detail="会话凭证无效")
        student_id = row["student_id"]
    staging.stage_upload(
        campaign_id=cid, session_id=session_id, student_id=student_id,
        slot=slot, repo_url=repo_url, cleaned_name=cleaned_name,
        cleaned_file=cleaned.file, attachment=attachment)
    return JSONResponse(
        status_code=202,
        content={"status": "pending_sync", "session_id": session_id,
                 "campaign_id": cid})


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
    slot < 0 为旧的整卷单件管线，保持不变。
    edge 实例（云端热备）改为暂存 + pending_sync，不写主库。"""
    if config.IS_EDGE:
        return _stage_on_edge(cid, session_id, slot, repo_url, cleaned,
                              attachment, x_session_token)
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
        file_hashes: list[tuple[str, str]] = []
        if attachment is not None and attachment.filename:
            attachment_path = save_zip_attachment(attachment, cid, session_id)
            file_hashes.append(("attachment", submission.sha256_of(attachment_path)))

        target_dir = _uploads_dir(cid, session_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        ext = ".md" if cleaned_name.endswith(".md") else ".jsonl"
        cleaned_path = target_dir / (f"cleaned_q{slot}{ext}" if slot >= 0
                                     else f"cleaned{ext}")
        digest = _write_with_hash(cleaned_path, cleaned.file)
        file_hashes.insert(0, ("cleaned", digest))
        # 落库收敛点（直连/P2P/转发三路径共用，含台账查重）
        updated, is_new = submission.commit_uploaded(
            conn, row, slot=slot, cleaned_path=cleaned_path,
            cleaned_name=cleaned_name, repo_url=repo_url or None,
            attachment_path=attachment_path, file_hashes=file_hashes,
            source="direct")
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
        # P2P 路径同样过收敛点（无文件哈希，台账自然不登记）
        updated, is_new = submission.commit_uploaded(
            conn, row, repo_url=body.repo_url or None, source="lan", lan=True)
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
