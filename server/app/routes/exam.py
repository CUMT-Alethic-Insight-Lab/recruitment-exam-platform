import collections
import json
import secrets
import sqlite3
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

from .. import auth, config, db, staging
from ..auth import require_admin, require_applicant
from ..constants import EXAM_TRACKS, MAX_ATTACHMENT_BYTES
from ..db import DEFAULT_EXAM_CONFIG
from ..schemas import ExamSessionOut, ExamVoid, PublicQuestionOut
from .campaigns import get_campaign_or_404, now_iso

router = APIRouter(prefix="/api/campaigns", tags=["exam"])

TRACK_KEYS = [t["key"] for t in EXAM_TRACKS]


class EnrollBody(BaseModel):
    # 开考二选一：track 缺省/空串 = 题库自选（须带 question_ids，会话记 track="selected"）；
    # track 非空走原有轨迹逻辑（向后兼容旧 kit）。
    track: str | None = None
    question_ids: list[str] | None = None
    domain: str | None = None
    student_id: str | None = None


def exam_config_for(row: sqlite3.Row) -> dict:
    cfg = {}
    try:
        cfg = json.loads(row["exam_config"] or "{}")
    except ValueError:
        cfg = {}
    merged = dict(DEFAULT_EXAM_CONFIG)
    merged.update(cfg)
    return merged


def pick_questions(conn: sqlite3.Connection, domains: list[str], track: str,
                   per_domain: int) -> list[sqlite3.Row]:
    picked: list[sqlite3.Row] = []
    for domain in domains:
        rows = conn.execute(
            "SELECT * FROM question WHERE domain = ? AND track = ? AND active = 1"
            " ORDER BY RANDOM() LIMIT ?",
            (domain, track, per_domain),
        ).fetchall()
        picked.extend(rows)
    return picked


def pick_selected_questions(conn: sqlite3.Connection,
                            question_ids: list[str]) -> list[sqlite3.Row]:
    """考生自选题（1-3 道且全部存在且 active），按提交顺序保序下发。"""
    if not 1 <= len(question_ids) <= 3:
        raise HTTPException(status_code=400, detail="自选题目数须为 1-3 道")
    rows: list[sqlite3.Row] = []
    for qid in question_ids:
        row = conn.execute("SELECT * FROM question WHERE id = ?", (qid,)).fetchone()
        if row is None or not row["active"]:
            raise HTTPException(status_code=400, detail=f"题目不存在或未开放: {qid}")
        rows.append(row)
    return rows


def check_selected_domains(qrows: list[sqlite3.Row],
                           applicant_domains: list[str]) -> None:
    """自选题目领域约束（track 无关）：题目领域 ⊆ 考生报名问卷领域。"""
    if any(r["domain"] not in applicant_domains for r in qrows):
        raise HTTPException(status_code=403, detail="只能选问卷里报过的领域")


def check_track3_domains(qrows: list[sqlite3.Row]) -> None:
    """自由创作自选（track3）额外约束：一个领域答一道题（领域互不重复）。

    领域归属校验已由 check_selected_domains 统一完成（403）；
    数量与 active 校验在 pick_selected_questions 已完成。
    """
    q_domains = [r["domain"] for r in qrows]
    if len(set(q_domains)) != len(q_domains):
        raise HTTPException(status_code=400, detail="同一领域只能选择一道题作答")


def save_zip_attachment(attachment: UploadFile, cid: str, sid: str) -> str:
    """校验并落盘 zip 附件（≤50MB）到 data/uploads/{cid}/{sid}/attachment.zip，
    返回落盘路径；超限先删半成品再 413。"""
    if not (attachment.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="附件须为 .zip 文件")
    target_dir = Path(config.DATA_DIR) / "uploads" / cid / sid
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
    return str(att_path)


def questions_to_dict(rows: list[sqlite3.Row]) -> list[dict]:
    return [{"id": r["id"], "domain": r["domain"], "title": r["title"], "body": r["body"]}
            for r in rows]


def get_session_or_404(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM exam_session WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="考试会话不存在")
    return row


def _row_to_out(row: sqlite3.Row) -> ExamSessionOut:
    return ExamSessionOut(
        id=row["id"],
        campaign_id=row["campaign_id"],
        student_id=row["student_id"],
        machine_id=row["machine_id"],
        track=row["track"],
        domain_tags=json.loads(row["domain_tags"]),
        questions_served=json.loads(row["questions_served"]),
        rerolls=row["rerolls"],
        status=row["status"],
        opened_at=row["opened_at"],
        uploaded_at=row["uploaded_at"],
        repo_url=row["repo_url"],
        cleaned_file_path=row["cleaned_file_path"],
        attachment_file_path=row["attachment_file_path"],
        student_name=row["student_name"] if "student_name" in row.keys() else None,
    )


def _serve_response(row: sqlite3.Row, cfg: dict) -> dict:
    qs = json.loads(row["questions_served"])
    track = row["track"]
    guide = cfg.get("track3_guide_text", "") if track == "track3" else ""
    return {
        "session_id": row["id"],
        "session_token": row["session_token"],
        "machine_id": row["machine_id"],
        "track": track,
        "domain_tags": json.loads(row["domain_tags"]),
        "questions": qs,
        "track3_guide_text": guide,
        "rerolls_left": max(0, cfg["reroll_limit"] - row["rerolls"]),
    }


@router.post("/{cid}/exam/enroll")
def enroll(cid: str, body: EnrollBody,
           student_id: str = Depends(require_applicant)):
    if body.student_id and body.student_id != student_id:
        raise HTTPException(status_code=403, detail="student_id 与登录会话不符")
    # 开考二选一：track 缺省/空串 = 题库自选（必须带 question_ids，会话记
    # track="selected"）；track 非空走原有轨迹逻辑（向后兼容旧 kit）。
    track = (body.track or "").strip()
    if track:
        if track not in TRACK_KEYS:
            raise HTTPException(status_code=400, detail=f"无效轨迹: {track}")
    elif not body.question_ids:
        raise HTTPException(status_code=400, detail="未选择作答方式")
    session_track = track or "selected"
    with db.connection() as conn:
        campaign = get_campaign_or_404(conn, cid)
        if campaign["status"] != "open":
            raise HTTPException(status_code=403, detail="当前批次已收口，无法开考")
        app = conn.execute(
            "SELECT * FROM application WHERE campaign_id = ? AND student_id = ?",
            (cid, student_id),
        ).fetchone()
        if app is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "no_application",
                        "message": "未找到该学号的报名记录，请先填写报名问卷（支持现场补录）"},
            )
        if not app["registration_complete"]:
            raise HTTPException(status_code=400, detail="请先完成报名问卷")
        existing = conn.execute(
            "SELECT * FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " AND status = 'draft' ORDER BY opened_at DESC LIMIT 1",
            (cid, student_id),
        ).fetchone()
        if existing is not None:
            cfg = exam_config_for(campaign)
            raise HTTPException(
                status_code=409,
                detail={"code": "already_enrolled",
                        "message": "已存在未交卷的考试会话，如需重考请由监考授权作废",
                        "session_id": existing["id"],
                        **{k: v for k, v in _serve_response(existing, cfg).items()}},
            )
        cfg = exam_config_for(campaign)
        domains = json.loads(app["domains"])
        if body.question_ids is not None:
            qrows = pick_selected_questions(conn, body.question_ids)
            # 凡自选题目（不论 track，含缺省）都限报名问卷领域内；track3 额外一域一题
            check_selected_domains(qrows, domains)
            if track == "track3":
                check_track3_domains(qrows)
            # 按领域交卷：自选开考的会话只记本次所选题目覆盖的领域（去重保序），
            # 供「该领域是否已交卷」与考生自查对账，避免「选了的和答了的领域不一致」
            session_domains = list(dict.fromkeys(r["domain"] for r in qrows))
        elif track == "track3":
            # M12 自由创作：track3 必须二选一——question_ids 选题（上面的分支）
            # 或 domain 自由发挥（不发题，只记领域）；不再随机发 track3 题。
            domain = (body.domain or "").strip()
            if not domain:
                raise HTTPException(status_code=400, detail="请先选择领域")
            if domain not in domains:
                raise HTTPException(status_code=400, detail="只能选择自己报名的领域")
            qrows = []
            session_domains = [domain]
        else:
            per_domain = (cfg["track1_per_domain"] if track == "track1"
                          else cfg["track2_per_domain"])
            qrows = pick_questions(conn, domains, track, per_domain)
            session_domains = domains
        # 已交卷约束：track1/track2/selected 一锤定音（有 uploaded 会话即 409）；
        # track3 按领域独立交卷——本次目标领域与已交会话 domain_tags 有交集才 409
        uploaded_rows = conn.execute(
            "SELECT domain_tags FROM exam_session WHERE campaign_id = ?"
            " AND student_id = ? AND status = 'uploaded'",
            (cid, student_id),
        ).fetchall()
        if track == "track3":
            done_domains: set[str] = set()
            for r in uploaded_rows:
                try:
                    done_domains.update(json.loads(r["domain_tags"] or "[]"))
                except ValueError:
                    continue
            overlap = [d for d in session_domains if d in done_domains]
            if overlap:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "domain_already_uploaded",
                            "message": "领域「" + "、".join(overlap)
                                       + "」已交卷，每个领域只能提交一次；"
                                         "如需重交请由监考作废原会话",
                            "domains": overlap})
        elif uploaded_rows:
            raise HTTPException(
                status_code=409,
                detail={"code": "already_uploaded",
                        "message": "该学号已交卷，如需重考请由监考作废原会话后再开考"})
        sid = uuid.uuid4().hex[:16]
        token = secrets.token_urlsafe(32)
        machine_id = uuid.uuid4().hex[:16]
        now = now_iso()
        conn.execute(
            "INSERT INTO exam_session (id, campaign_id, student_id, session_token,"
            " machine_id, track, domain_tags, questions_served, status, opened_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
            (sid, cid, student_id, token, machine_id, session_track,
             json.dumps(session_domains, ensure_ascii=False),
             json.dumps(questions_to_dict(qrows), ensure_ascii=False), now),
        )
        row = get_session_or_404(conn, sid)
        return _serve_response(row, cfg)


@router.post("/{cid}/exam/reroll")
def reroll(cid: str, body: dict, x_session_token: str | None = Header(default=None),
           login_student_id: str = Depends(require_applicant)):
    sid = str(body.get("session_id", ""))
    if not sid or not x_session_token:
        raise HTTPException(status_code=400, detail="缺少会话凭证")
    with db.connection() as conn:
        campaign = get_campaign_or_404(conn, cid)
        row = get_session_or_404(conn, sid)
        if row["campaign_id"] != cid:
            raise HTTPException(status_code=404, detail="会话不属于该批次")
        if row["student_id"] != login_student_id:
            raise HTTPException(status_code=403, detail="考试会话不属于当前登录考生")
        if row["session_token"] != x_session_token:
            raise HTTPException(status_code=401, detail="会话凭证无效")
        if row["status"] != "draft":
            raise HTTPException(status_code=409, detail="会话已交卷，不能换题")
        cfg = exam_config_for(campaign)
        if row["rerolls"] >= cfg["reroll_limit"]:
            raise HTTPException(status_code=409, detail=f"已用完换题次数（{cfg['reroll_limit']} 次）")
        track = row["track"]
        if track == "track3":
            raise HTTPException(status_code=400, detail="自由创作型轨迹无需换题")
        if track == "selected":
            raise HTTPException(status_code=400, detail="题库自选会话不支持换题")
        domains = json.loads(row["domain_tags"])
        per_domain = cfg["track1_per_domain"] if track == "track1" else cfg["track2_per_domain"]
        qrows = pick_questions(conn, domains, track, per_domain)
        conn.execute(
            "UPDATE exam_session SET questions_served = ?, rerolls = rerolls + 1 WHERE id = ?",
            (json.dumps(questions_to_dict(qrows), ensure_ascii=False), sid),
        )
        updated = get_session_or_404(conn, sid)
        return _serve_response(updated, cfg)


@router.post("/{cid}/exam/void", response_model=ExamSessionOut)
def void_sessions(cid: str, body: ExamVoid, _: None = Depends(require_admin)):
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        cur = conn.execute(
            "UPDATE exam_session SET status = 'voided'"
            " WHERE campaign_id = ? AND student_id = ? AND status IN ('draft', 'uploaded')",
            (cid, body.student_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="该学号没有可作废的考试会话")
        row = conn.execute(
            "SELECT * FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " AND status = 'voided' ORDER BY opened_at DESC LIMIT 1",
            (cid, body.student_id),
        ).fetchone()
        return _row_to_out(row)


@router.get("/{cid}/exam/sessions", response_model=list[ExamSessionOut])
def list_sessions(cid: str, student_id: str | None = None, _: None = Depends(require_admin)):
    sql = ("SELECT s.*, a.name AS student_name FROM exam_session s"
           " LEFT JOIN application a ON s.campaign_id = a.campaign_id"
           " AND s.student_id = a.student_id WHERE s.campaign_id = ?")
    params: list = [cid]
    if student_id:
        sql += " AND s.student_id = ?"
        params.append(student_id)
    sql += " ORDER BY s.opened_at DESC"
    with db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = [_row_to_out(r) for r in rows]
    if config.IS_EDGE:
        items = _merge_staged_sessions(cid, student_id, items)
    return items


def _merge_staged_sessions(cid: str, student_id: str | None,
                           items: list[ExamSessionOut]) -> list[ExamSessionOut]:
    """edge 模式：把 staging 暂存件合入会话列表，标 pending_sync（待同步）。

    暂存件比热备库新：库里还是 draft 的会话直接改标 pending_sync；
    库里没有（同步窗口内新会话）补合成条目；已 uploaded/voided 以主库为准。"""
    by_id = {s.id: s for s in items}
    for meta in staging.list_staged(campaign_id=cid, student_id=student_id):
        sid = meta.get("session_id") or ""
        existing = by_id.get(sid)
        if existing is not None:
            if existing.status == "draft":
                existing.status = "pending_sync"
            continue
        items.append(ExamSessionOut(
            id=sid, campaign_id=cid,
            student_id=meta.get("student_id") or "",
            machine_id="", track="", domain_tags=[], questions_served=[],
            rerolls=0, status="pending_sync",
            opened_at=meta.get("staged_at") or "", uploaded_at=None,
            repo_url=meta.get("repo_url") or None))
    items.sort(key=lambda s: s.opened_at or "", reverse=True)
    return items


@router.get("/{cid}/exam/questions", response_model=list[PublicQuestionOut])
def public_questions(cid: str, _: str = Depends(require_applicant)):
    """考生可见的全部 active 题目（仅题面字段，绝不含 meta/评审字段）。"""
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        rows = conn.execute(
            "SELECT id, domain, track, title, body FROM question WHERE active = 1"
            " ORDER BY domain, track, id"
        ).fetchall()
        return [PublicQuestionOut(id=r["id"], domain=r["domain"], track=r["track"],
                                  title=r["title"], body=r["body"]) for r in rows]


@router.get("/{cid}/exam/my_sessions")
def my_sessions(cid: str, student_id: str = Depends(require_applicant)):
    """考生自查本人本届的考试会话列表（按 domain_tags 对账各领域完成状态）。

    返回 [{session_id, track, domain_tags, status, opened_at, uploaded_at,
    repo_url}]，新的在前；已作废会话保留并以 status="voided" 标注
    （页面只把 uploaded 计为「已提交」）。"""
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        rows = conn.execute(
            "SELECT id, track, domain_tags, status, opened_at, uploaded_at, repo_url"
            " FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " ORDER BY opened_at DESC",
            (cid, student_id),
        ).fetchall()
    out = []
    for r in rows:
        try:
            tags = json.loads(r["domain_tags"] or "[]")
        except ValueError:
            tags = []
        out.append({
            "session_id": r["id"],
            "track": r["track"],
            "domain_tags": tags,
            "status": r["status"],
            "opened_at": r["opened_at"],
            "uploaded_at": r["uploaded_at"],
            "repo_url": r["repo_url"],
        })
    return out


P2P_KEY_LIMIT_PER_HOUR = 10
P2P_KEY_TTL_HOURS = 4
_p2p_key_calls: dict[str, collections.deque] = {}


def _check_p2p_key_rate(student_id: str) -> None:
    """考生维度内存限流（10 次/小时）。登录限流是 login_attempt 表的锁定语义，
    不通用，故此处单独实现。"""
    now = time.monotonic()
    calls = _p2p_key_calls.setdefault(student_id, collections.deque())
    while calls and calls[0] <= now - 3600:
        calls.popleft()
    if len(calls) >= P2P_KEY_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    calls.append(now)


def _p2p_unavailable() -> HTTPException:
    # 客户端据此回退普通 HTTPS 上传；不向外泄露 API Key 与异常细节
    return HTTPException(status_code=503, detail="p2p unavailable")


def _request_preauthkey() -> str:
    expiration = (datetime.now(timezone.utc)
                  + timedelta(hours=P2P_KEY_TTL_HOURS)).isoformat().replace("+00:00", "Z")
    req = urllib.request.Request(
        f"{config.HEADSCALE_URL}/api/v1/preauthkey",
        data=json.dumps({
            "user": config.HEADSCALE_USER_ID,
            "reusable": False,
            "ephemeral": True,
            "expiration": expiration,
            "aclTags": ["tag:candidate"],
        }).encode(),
        headers={
            "Authorization": f"Bearer {config.HEADSCALE_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    return data["preAuthKey"]["key"]


@router.post("/{cid}/exam/p2p-key")
def exam_p2p_key(cid: str, student_id: str = Depends(require_applicant)):
    """向考生下发短时 Headscale preauthkey，供机试包接入 P2P mesh 直传实验室主机。"""
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
    _check_p2p_key_rate(student_id)
    if not config.HEADSCALE_API_KEY or not config.LAB_TAILNET_IP:
        raise _p2p_unavailable()
    try:
        key = _request_preauthkey()
    except Exception:
        raise _p2p_unavailable() from None
    with db.connection() as conn:
        auth.write_audit(conn, student_id, "exam_p2p_key", target=cid,
                         detail="headscale preauthkey issued")
    return {
        "login_server": config.HEADSCALE_URL,
        "auth_key": key,
        "lab_ip": config.LAB_TAILNET_IP,
        "receiver_port": 8897,
        "api_port": 8901,
    }


@router.post("/{cid}/applications/me/project", response_model=ExamSessionOut)
def submit_project(cid: str, repo_url: str = Form(...),
                   attachment: UploadFile | None = File(None),
                   student_id: str = Depends(require_applicant)):
    """自有项目直提（开考二选一）：GitHub 仓库链接 + 可选 zip 附件（≤50MB），
    直接生成已交卷会话（track='project'）。开考二选一下任何完成报名问卷的考生
    均可提交，不再限于问卷 exam_mode=own_project。"""
    repo_url = repo_url.strip()
    if not repo_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="repo_url 须以 http(s):// 开头")
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        app = conn.execute(
            "SELECT * FROM application WHERE campaign_id = ? AND student_id = ?",
            (cid, student_id),
        ).fetchone()
        if app is None:
            raise HTTPException(status_code=404, detail="未找到该学号的报名记录")
        if not app["registration_complete"]:
            raise HTTPException(status_code=400, detail="请先完成报名问卷")
        existing = conn.execute(
            "SELECT 1 FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " AND status != 'voided'",
            (cid, student_id),
        ).fetchone()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "already_uploaded",
                        "message": "已存在考试会话，请勿重复提交仓库链接"},
            )
        sid = uuid.uuid4().hex[:16]
        token = secrets.token_urlsafe(32)
        now = now_iso()
        attachment_path = None
        if attachment is not None and attachment.filename:
            attachment_path = save_zip_attachment(attachment, cid, sid)
        conn.execute(
            "INSERT INTO exam_session (id, campaign_id, student_id, session_token,"
            " machine_id, track, domain_tags, questions_served, status, opened_at,"
            " uploaded_at, repo_url, attachment_file_path)"
            " VALUES (?, ?, ?, ?, 'web', 'project', '[]', '[]', 'uploaded', ?, ?, ?, ?)",
            (sid, cid, student_id, token, now, now, repo_url, attachment_path),
        )
        conn.execute(
            "UPDATE application SET status = 'tested', updated_at = ?"
            " WHERE campaign_id = ? AND student_id = ?",
            (now, cid, student_id),
        )
        row = get_session_or_404(conn, sid)
        return _row_to_out(row)
