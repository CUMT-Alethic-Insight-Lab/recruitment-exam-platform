import json
import secrets
import sqlite3
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .. import auth, db
from ..auth import require_admin, require_applicant
from ..constants import EXAM_TRACKS
from ..db import DEFAULT_EXAM_CONFIG
from ..schemas import ExamSessionOut, ExamVoid, ProjectSubmitBody, PublicQuestionOut
from .campaigns import get_campaign_or_404, now_iso

router = APIRouter(prefix="/api/campaigns", tags=["exam"])

TRACK_KEYS = [t["key"] for t in EXAM_TRACKS]


class EnrollBody(BaseModel):
    track: str
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


def check_track3_domains(qrows: list[sqlite3.Row],
                         applicant_domains: list[str]) -> None:
    """自由创作自选（track3）领域约束：一个领域答一道题。

    ①题目领域必须都在考生报名领域内；②题目领域互不重复；③1~3 题（此处只管前两条，
    数量与 active 校验在 pick_selected_questions 已完成）。
    """
    q_domains = [r["domain"] for r in qrows]
    if any(d not in applicant_domains for d in q_domains):
        raise HTTPException(status_code=400, detail="只能选择自己报名领域内的题目")
    if len(set(q_domains)) != len(q_domains):
        raise HTTPException(status_code=400, detail="同一领域只能选择一道题作答")


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
    if body.track not in TRACK_KEYS:
        raise HTTPException(status_code=400, detail=f"无效轨迹: {body.track}")
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
        if app["exam_mode"] == "own_project":
            raise HTTPException(
                status_code=400,
                detail={"code": "own_project_no_enroll",
                        "message": "个人项目轨请在报名页提交仓库链接"},
            )
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
        prev_uploaded = conn.execute(
            "SELECT 1 FROM exam_session WHERE campaign_id = ? AND student_id = ?"
            " AND status = 'uploaded'",
            (cid, student_id),
        ).fetchone()
        if prev_uploaded is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "already_uploaded",
                        "message": "该学号已交卷，如需重考请由监考作废原会话后再开考"},
            )
        cfg = exam_config_for(campaign)
        domains = json.loads(app["domains"])
        if body.question_ids is not None:
            qrows = pick_selected_questions(conn, body.question_ids)
            if body.track == "track3":
                check_track3_domains(qrows, domains)
            session_domains = domains
        elif body.track == "track3":
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
            per_domain = (cfg["track1_per_domain"] if body.track == "track1"
                          else cfg["track2_per_domain"])
            qrows = pick_questions(conn, domains, body.track, per_domain)
            session_domains = domains
        sid = uuid.uuid4().hex[:16]
        token = secrets.token_urlsafe(32)
        machine_id = uuid.uuid4().hex[:16]
        now = now_iso()
        conn.execute(
            "INSERT INTO exam_session (id, campaign_id, student_id, session_token,"
            " machine_id, track, domain_tags, questions_served, status, opened_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
            (sid, cid, student_id, token, machine_id, body.track,
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
        return [_row_to_out(r) for r in rows]


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


@router.post("/{cid}/applications/me/project", response_model=ExamSessionOut)
def submit_project(cid: str, body: ProjectSubmitBody,
                   student_id: str = Depends(require_applicant)):
    """个人项目轨考生直提 GitHub 仓库链接，直接生成已交卷会话。"""
    repo_url = body.repo_url.strip()
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
        if app["exam_mode"] != "own_project":
            raise HTTPException(
                status_code=400,
                detail={"code": "not_own_project",
                        "message": "仅个人项目（GitHub 仓库直提）考生可提交仓库链接"},
            )
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
        conn.execute(
            "INSERT INTO exam_session (id, campaign_id, student_id, session_token,"
            " machine_id, track, domain_tags, questions_served, status, opened_at,"
            " uploaded_at, repo_url)"
            " VALUES (?, ?, ?, ?, 'web', 'project', '[]', '[]', 'uploaded', ?, ?, ?)",
            (sid, cid, student_id, token, now, now, repo_url),
        )
        conn.execute(
            "UPDATE application SET status = 'tested', updated_at = ?"
            " WHERE campaign_id = ? AND student_id = ?",
            (now, cid, student_id),
        )
        row = get_session_or_404(conn, sid)
        return _row_to_out(row)
