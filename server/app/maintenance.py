"""过期批次数据回收：删除 uploads 目录与关联行，并将批次置为 closed。

用法：
    python -m app.maintenance            # 以当前时间执行
或由 systemd oneshot 单元定时调用（见 deploy/alethic-recruitment-purge.timer）。
"""

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

# question 表（见 db.py SCHEMA）没有 campaign 外键/关联列，属全局题库，不随批次删除；
# exam_score 通过 session_id 关联 exam_session（无 campaign_id 列），需随会话先删；
# 表可能不存在于旧库，删除前先探测。


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def purge_expired(now_iso_str: str | None = None) -> dict:
    db.init_db()  # 幂等：空库/缺表时先建表（CREATE TABLE IF NOT EXISTS）
    now = datetime.now(timezone.utc) if now_iso_str is None else _aware(
        datetime.fromisoformat(now_iso_str))
    purged: list[str] = []
    skipped: list[str] = []
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM campaign").fetchall()
        for row in rows:
            cid = row["id"]
            try:
                expires_at = _aware(datetime.fromisoformat(row["expires_at"]))
            except ValueError:
                skipped.append(cid)
                continue
            if expires_at >= now:
                skipped.append(cid)
                continue
            uploads_dir = Path(config.DATA_DIR) / "uploads" / cid
            related = []
            for table in ("application", "exam_session", "interview_score",
                          "interview_slot", "login_session", "question"):
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if "campaign_id" in cols:
                    related.append(table)
            pending = any(conn.execute(
                f"SELECT 1 FROM {table} WHERE campaign_id = ? LIMIT 1", (cid,)
            ).fetchone() for table in related)
            if row["status"] == "closed" and not uploads_dir.exists() and not pending:
                skipped.append(cid)
                continue
            if _table_exists(conn, "exam_score"):
                conn.execute(
                    "DELETE FROM exam_score WHERE session_id IN"
                    " (SELECT id FROM exam_session WHERE campaign_id = ?)", (cid,))
            # 子表先于 application 删除；account 与 membership 是跨批次长期数据，不回收。
            for table in ("interview_score", "exam_session", "interview_slot",
                          "login_session", "question", "application"):
                if table in related:
                    conn.execute(f"DELETE FROM {table} WHERE campaign_id = ?", (cid,))
            conn.execute("UPDATE campaign SET status = 'closed' WHERE id = ?", (cid,))
            purged.append(cid)
    for cid in purged:
        shutil.rmtree(Path(config.DATA_DIR) / "uploads" / cid, ignore_errors=True)
    return {"purged": purged, "skipped": skipped}


if __name__ == "__main__":
    summary = purge_expired()
    purged = ", ".join(summary["purged"]) if summary["purged"] else "无"
    print(f"[maintenance] 已回收批次：{purged}")
    print(f"[maintenance] 未到期/跳过批次：{len(summary['skipped'])} 个")
