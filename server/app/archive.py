"""exam-archive 即时备份（M22，仅 authoritative/lab）。

任何会话转为 uploaded（直连交卷 / P2P 标记 / 暂存收口 / 项目直提）后，
trigger() 触发后台归档，绝不阻塞交卷响应：

- 目标：本地 git 克隆 ARCHIVE_REPO_DIR（org 私有仓 exam-archive），
  目录 {campaign_id}/{student_id}/
- 清洗件用 cleaned_report 转 md（逐题 slot 各一份 cleaned_qN.md，单槽
  cleaned.md，头部带学号/轨迹/开考/交卷时间——与下载接口同口径）
- 有 repo_url → 该目录写 README.md 贴 GitHub 链接
- 附件为 zip → 解压成 attachment/ 目录入库（不存 zip 本体，防 zip-slip）；
  非 zip 附件原样复制进 attachment/
- git add -A && commit && push（无变更跳过）；push 用 ARCHIVE_GITHUB_TOKEN
  内嵌 https URL 临时传，token 不落库不落日志（失败原因脱敏）
- push 失败 → 追加 data/archive_queue.jsonl 待推队列；app lifespan 后台任务
  每 10min 扫 flush_queue() 补推（同一克隆一次 push 即清全部待推）

缺 ARCHIVE_ENABLED（默认关）/ ARCHIVE_REPO_DIR 静默跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import cleaned_report, config, db

# 测试置 False 同步执行；生产默认后台线程，绝不阻塞交卷响应
BACKGROUND = True
GIT_TIMEOUT = 120


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path() -> Path:
    return Path(config.DATA_DIR) / "archive_queue.jsonl"


def _enqueue(session_id: str, campaign_id: str, student_id: str,
             reason: str) -> None:
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": session_id,
        "campaign_id": campaign_id,
        "student_id": student_id,
        "queued_at": _now_iso(),
        "reason": reason,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_queue() -> list[dict]:
    path = _queue_path()
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


def _enabled() -> bool:
    return bool(config.ARCHIVE_ENABLED and config.ARCHIVE_REPO_DIR
                and not config.IS_EDGE)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT)


def _mask(text: str) -> str:
    token = config.ARCHIVE_GITHUB_TOKEN
    return text.replace(token, "***") if token else text


def _push_url(repo: Path) -> str | None:
    proc = _git(repo, "remote", "get-url", "origin")
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    token = config.ARCHIVE_GITHUB_TOKEN
    if token and url.startswith("https://"):
        url = "https://x-access-token:" + token + "@" + url[len("https://"):]
    return url or None


def push(repo: Path | None = None) -> None:
    """push 当前分支到 origin（token 仅内嵌临时 URL，不写入 git 配置）；
    失败抛 RuntimeError（输出已脱敏）。"""
    repo = repo or Path(config.ARCHIVE_REPO_DIR)
    url = _push_url(repo)
    if not url:
        raise RuntimeError("exam-archive 克隆缺少 origin remote")
    proc = subprocess.run(
        ["git", "-C", str(repo), "push", url, "HEAD"],
        capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:300]
        raise RuntimeError(_mask(detail))


def _extract_zip(zpath: Path, dest: Path) -> None:
    """解压 zip 到 dest，拒绝绝对路径与 .. 成员（zip-slip）。"""
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or parts[0].endswith(":") or ".." in parts:
                continue
            if info.is_dir():
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def trigger(session_id: str) -> None:
    """交卷收敛点之后调用：后台归档（BACKGROUND=False 时同步，测试用）。"""
    if not _enabled():
        return
    if BACKGROUND:
        threading.Thread(
            target=_safe_archive, args=(session_id,), daemon=True).start()
    else:
        _safe_archive(session_id)


def _safe_archive(session_id: str) -> None:
    try:
        archive_session(session_id)
    except Exception as exc:
        print(f"[WARN] exam-archive 归档异常（会话 {session_id}）: {_mask(str(exc))}")


def archive_session(session_id: str) -> dict:
    """把单个已交卷会话归档进 exam-archive 克隆并 commit/push。"""
    if not _enabled():
        return {"archived": False, "reason": "disabled"}
    repo = Path(config.ARCHIVE_REPO_DIR)
    if not (repo / ".git").is_dir():
        print(f"[WARN] exam-archive 克隆不存在: {repo}")
        return {"archived": False, "reason": "no_repo"}
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM exam_session WHERE id = ?", (session_id,)).fetchone()
    if row is None or row["status"] != "uploaded":
        return {"archived": False, "reason": "not_uploaded"}
    cid, student = row["campaign_id"], row["student_id"]
    target = repo / cid / student
    target.mkdir(parents=True, exist_ok=True)
    header = [
        ("学号", student),
        ("轨迹", row["track"]),
        ("开考时间", row["opened_at"]),
        ("交卷时间", row["uploaded_at"] or ""),
    ]
    wrote = False
    try:
        slots = json.loads(row["slots"] or "{}")
    except ValueError:
        slots = {}
    if slots:
        for key in sorted(slots, key=lambda k: int(k) if k.lstrip("-").isdigit() else 0):
            entry = slots[key]
            path = (entry or {}).get("path") if isinstance(entry, dict) else None
            if not path or not Path(path).is_file():
                continue
            slot_header = list(header)
            if key.lstrip("-").isdigit():
                slot_header.append(("题号", f"第 {int(key) + 1} 题"))
            md = cleaned_report.records_to_markdown(
                cleaned_report.load_records(path), slot_header)
            (target / f"cleaned_q{key}.md").write_text(md, encoding="utf-8")
            wrote = True
    elif row["cleaned_file_path"] and Path(row["cleaned_file_path"]).is_file():
        md = cleaned_report.records_to_markdown(
            cleaned_report.load_records(row["cleaned_file_path"]), header)
        (target / "cleaned.md").write_text(md, encoding="utf-8")
        wrote = True
    if row["repo_url"]:
        (target / "README.md").write_text(
            f"# {cid}/{student} 机试归档\n\n"
            f"- GitHub 仓库：{row['repo_url']}\n"
            f"- 考试会话：{session_id}\n"
            f"- 交卷时间：{row['uploaded_at'] or ''}\n",
            encoding="utf-8")
        wrote = True
    att = row["attachment_file_path"]
    if att and Path(att).is_file():
        att_dir = target / "attachment"
        att_dir.mkdir(exist_ok=True)
        if att.endswith(".zip") and zipfile.is_zipfile(att):
            _extract_zip(Path(att), att_dir)
        else:
            shutil.copy2(att, att_dir / Path(att).name)
        wrote = True
    if not wrote:
        return {"archived": False, "reason": "nothing_to_archive"}
    _git(repo, "add", "-A")
    if not _git(repo, "status", "--porcelain").stdout.strip():
        return {"archived": True, "committed": False, "pushed": False}
    proc = _git(repo, "-c", "user.name=alethic-archive-bot",
                "-c", "user.email=archive-bot@alethicinsight.com",
                "commit", "-m", f"archive: {cid}/{student} 会话 {session_id}")
    if proc.returncode != 0:
        raise RuntimeError(_mask((proc.stderr.strip()
                                  or proc.stdout.strip())[:300]))
    try:
        push(repo)
    except Exception as exc:
        _enqueue(session_id, cid, student, _mask(str(exc)))
        print(f"[WARN] exam-archive push 失败，已入待推队列（会话 {session_id}）")
        return {"archived": True, "committed": True, "pushed": False}
    return {"archived": True, "committed": True, "pushed": True}


def flush_queue() -> dict:
    """补推待推队列：所有失败会话的 commit 都在同一克隆里，
    一次 push 成功即清空整个队列。"""
    entries = load_queue()
    if not entries:
        return {"pushed": False, "pending": 0}
    if not _enabled():
        return {"pushed": False, "pending": len(entries)}
    repo = Path(config.ARCHIVE_REPO_DIR)
    if not (repo / ".git").is_dir():
        return {"pushed": False, "pending": len(entries)}
    try:
        push(repo)
    except Exception as exc:
        print(f"[WARN] exam-archive 队列补推失败: {_mask(str(exc))}")
        return {"pushed": False, "pending": len(entries)}
    _queue_path().unlink(missing_ok=True)
    return {"pushed": True, "cleared": len(entries), "pending": 0}
