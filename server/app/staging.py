"""edge（云端 VPS）交卷暂存（M21）。

edge 模式下交卷端点不写主库：multipart 原样落 data/staging/{session_id}/，
响应 pending_sync；由 `python -m app.staging_forward`（systemd timer 每 30min）
转发 lab 权威库收口，lab 应答成功后整目录删除——云端不留数据。
暂存不加密，沿用 data/uploads 的朴素落盘风格。

目录布局：
    data/staging/{session_id}/
        meta.json     # campaign/student/repo_url + uploads{slot: 各文件 sha256...}
        cleaned.jsonl | cleaned_q{N}.jsonl | cleaned.md ...
        attachment.zip（或原始扩展名）
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

# session_id 来自客户端表单，落盘前必须约束字符集，防路径穿越
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def staging_root() -> Path:
    return Path(config.DATA_DIR) / "staging"


def staging_dir(session_id: str) -> Path:
    return staging_root() / session_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_with_hash(target: Path, fileobj) -> str:
    h = hashlib.sha256()
    with target.open("wb") as f:
        while chunk := fileobj.read(1024 * 1024):
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()


def read_meta(sdir: Path) -> dict | None:
    path = sdir / "meta.json"
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


def stage_upload(*, campaign_id: str, session_id: str, student_id: str,
                 slot: int, repo_url: str, cleaned_name: str, cleaned_file,
                 attachment=None) -> Path:
    """一次交卷 multipart 落盘并合并 meta.json（同 slot 覆盖，逐题幂等）。

    返回暂存目录。attachment 为可选的 UploadFile（原样保存，不强制 zip）。"""
    sdir = staging_dir(session_id)
    sdir.mkdir(parents=True, exist_ok=True)
    ext = ".md" if cleaned_name.endswith(".md") else ".jsonl"
    rel = f"cleaned_q{slot}{ext}" if slot >= 0 else f"cleaned{ext}"
    digest = _write_with_hash(sdir / rel, cleaned_file)
    files = [{"field": "cleaned", "name": cleaned_name, "path": rel,
              "sha256": digest}]
    if attachment is not None and attachment.filename:
        aname = Path(attachment.filename).name
        arel = "attachment" + (Path(aname).suffix or ".zip")
        adigest = _write_with_hash(sdir / arel, attachment.file)
        files.append({"field": "attachment", "name": aname, "path": arel,
                      "sha256": adigest})
    meta = read_meta(sdir) or {
        "campaign_id": campaign_id,
        "session_id": session_id,
        "student_id": student_id,
        "repo_url": "",
        "staged_at": _now_iso(),
        "uploads": {},
    }
    if student_id and not meta.get("student_id"):
        meta["student_id"] = student_id
    if repo_url:
        meta["repo_url"] = repo_url
    meta["uploads"][str(slot)] = {
        "slot": slot,
        "staged_at": _now_iso(),
        "files": files,
    }
    (sdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return sdir


def list_staged(campaign_id: str | None = None,
                student_id: str | None = None) -> list[dict]:
    """列出全部暂存 meta（可按批次/学号过滤），供管理端合并 pending_sync
    与 staging_forward 逐个转发。"""
    root = staging_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not SESSION_ID_RE.match(d.name):
            continue
        meta = read_meta(d)
        if not meta:
            continue
        if campaign_id and meta.get("campaign_id") != campaign_id:
            continue
        if student_id and meta.get("student_id") not in (None, "", student_id):
            continue
        meta.setdefault("session_id", d.name)
        out.append(meta)
    return out
