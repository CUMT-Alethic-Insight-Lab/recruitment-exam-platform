"""交卷落库收敛点 + 查重台账（M21）。

直连上传（routes/uploads.upload_session）、局域网 P2P 标记（lan-uploaded）、
云端暂存转发收口（routes/internal.ingest-staged）三路径统一经
commit_uploaded 落库：

- 台账查重：data/ingest_ledger.jsonl（append-only），按
  (session_id, slot, sha256) 三元组判重——同件重发直接幂等跳过
  （is_new=False，不重复写库）；不同会话/槽位的相同内容不互相误伤
- 新件：exam_session 置 uploaded（整卷写 cleaned_file_path / 逐题记 slots /
  P2P 标记置空路径）、application 联动 tested、台账登记 source
- 文件归位（写 data/uploads/{cid}/{sid}/）由调用方在调用前完成
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_of(path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ledger_path() -> Path:
    return Path(config.DATA_DIR) / "ingest_ledger.jsonl"


def load_ledger() -> list[dict]:
    path = ledger_path()
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def ledger_seen(session_id: str, slot: int, sha256: str) -> bool:
    """同 (session, slot, hash) 是否已登记——三路径重发/竞态的幂等判据。"""
    if not sha256:
        return False
    slot_key = str(slot)
    return any(
        e.get("session_id") == session_id
        and str(e.get("slot")) == slot_key
        and e.get("sha256") == sha256
        for e in load_ledger()
    )


def ledger_append(*, session_id: str, slot: int, sha256: str, source: str) -> None:
    if not sha256:
        return
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": session_id,
        "slot": slot,
        "sha256": sha256,
        "source": source,
        "received_at": _now_iso(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def commit_uploaded(conn: sqlite3.Connection, row: sqlite3.Row, *,
                    slot: int = -1, cleaned_path: Path | None = None,
                    cleaned_name: str = "", repo_url: str | None = None,
                    attachment_path: str | None = None,
                    file_hashes: list[tuple[str, str]] = (),
                    source: str = "direct", lan: bool = False,
                    uploaded_at: str | None = None) -> tuple[sqlite3.Row, bool]:
    """交卷落库收敛点。返回 (更新后的会话行, 是否本次新落库)。

    台账命中（同 session+slot+hash）→ 幂等跳过，返回 (当前行, False)；
    否则按 slot/lan 形态落库并登记台账（source: direct|lan|staged）。
    """
    cleaned_hash = next((h for field, h in file_hashes if field == "cleaned"), "")
    if ledger_seen(row["id"], slot, cleaned_hash):
        return row, False
    uploaded_at = uploaded_at or _now_iso()
    sid = row["id"]
    if lan:
        conn.execute(
            "UPDATE exam_session SET status = 'uploaded', uploaded_at = ?,"
            " cleaned_file_path = NULL, attachment_file_path = NULL, repo_url = ?"
            " WHERE id = ?",
            (uploaded_at, repo_url or None, sid),
        )
    elif slot >= 0:
        try:
            slots = json.loads(row["slots"] or "{}")
        except ValueError:
            slots = {}
        slots[str(slot)] = {
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
             repo_url or None, attachment_path, sid),
        )
    else:
        conn.execute(
            "UPDATE exam_session SET status = 'uploaded', uploaded_at = ?,"
            " cleaned_file_path = ?, repo_url = ?, attachment_file_path = ? WHERE id = ?",
            (uploaded_at, str(cleaned_path), repo_url or None,
             attachment_path, sid),
        )
    conn.execute(
        "UPDATE application SET status = 'tested', updated_at = ?"
        " WHERE campaign_id = ? AND student_id = ? AND status = 'applied'",
        (uploaded_at, row["campaign_id"], row["student_id"]),
    )
    for field, digest in file_hashes:
        ledger_append(session_id=sid, slot=slot, sha256=digest, source=source)
    updated = conn.execute(
        "SELECT * FROM exam_session WHERE id = ?", (sid,)).fetchone()
    return updated, True
