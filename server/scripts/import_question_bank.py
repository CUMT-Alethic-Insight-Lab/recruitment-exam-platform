"""题库 v5 解析并幂等导入 question 表。

题库源文件 data/question_bank_v5.md 按领域分节，题目标题前缀决定
role/track/active（口径与 app/constants.py、面试备课卡契约一致）：
    正选→正选/track1/active=1    备选→备选/track1/active=1
    第三题/开放题→开放/track3/active=1
    实操/实操位→实操/track2/active=1
    加试→加试/track1/active=1（默认下发）
    梯队专用→track1/active=0
    附三（领域九，AI 安全）→domain=ai_safety、role=面试素材/active=0

题目内部的「出题依据/考察点/优秀回答特征/翻车模式/出题人备注及各类评审
专用节」全部进 meta（JSON），题面只存 body。按 bank_id 幂等 upsert：
已存在的 bank_id 更新内容字段并刷新 updated_at，不动 id/created_at；
不在本次题库中的旧题不动。

用法：
    python scripts/import_question_bank.py                # 导入开发库
    python scripts/import_question_bank.py --db PATH      # 指定 SQLite 文件
    python scripts/import_question_bank.py --dry-run      # 只解析统计，不写库
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MD = SERVER_DIR / "data" / "question_bank_v5.md"

DOMAIN_MAP = {
    "领域一": "llm_arch",
    "领域二": "cv_arch",
    "领域三": "vlm_align",
    "领域四": "rl",
    "领域五": "agent",
    "领域六": "memory",
    "领域七": "infra",
    "领域九": "ai_safety",  # 附三内的原领域九，整域迁为面试素材
}

# (标题前缀, role, track)；梯队专用与附三面试素材置 active=0，track 归入 track1
PREFIX_RULES = [
    ("正选", "正选", "track1"),
    ("备选", "备选", "track1"),
    ("第三题", "开放", "track3"),
    ("开放题", "开放", "track3"),
    ("实操位", "实操", "track2"),
    ("实操", "实操", "track2"),
    ("加试", "加试", "track1"),
]

# 加粗小节标签 → meta/body 字段；其余标签（锚点清单、评分参考、口径警示等
# 及一切含「评审专用」的小节）整体进 reviewer_notes
FIELD_LABELS = {
    "题面": "body",
    "题目": "body",
    "配套材料": "body",
    "出题依据": "basis",
    "考察点": "test_points",
    "优秀回答": "pass_signals",
    "常见翻车模式": "failure_modes",
    "翻车模式": "failure_modes",
}
REVIEWER_LABELS = (
    "出题人备注", "锚点清单", "本题锚点清单", "评分参考", "评审抓手",
    "面试追问弹药", "控时自检", "出题人参考答案", "口径警示", "评审专用",
)

BOLD_RE = re.compile(r"^\*\*(.+?)\*\*")
BANK_ID_RE = re.compile(r"[A-Z]\d{1,2}")
SUBHEADING_RE = re.compile(r"^#{4,6}\s*(.+?)\s*#*\s*$")

META_KEYS = ("basis", "test_points", "pass_signals", "failure_modes", "reviewer_notes")

LEAK_WORDS = (
    "评审专用", "出题人备注", "翻车模式", "优秀回答", "不下发",
    "评分参考", "评审抓手", "面试追问", "锚点清单",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _resolve_label(label: str) -> str | None:
    """小节标签 → body/basis/test_points/pass_signals/failure_modes/reviewer。

    先按语义关键词（出题依据/考察点/…）匹配——变体 B 的 #### 小节虽都标注
    「评审专用，不下发」，但仍需落到各自语义字段；纯评审节（评分参考、
    锚点清单等）与一切含「评审专用」的未知标签归 reviewer_notes。
    """
    for key, field in FIELD_LABELS.items():
        if label.startswith(key):
            return field
    if "评审专用" in label:
        return "reviewer"
    for key in REVIEWER_LABELS:
        if label.startswith(key):
            return "reviewer"
    return None


def _classify_heading(heading: str, domain: str) -> tuple[str, str, int]:
    role, track = "", "track1"
    for prefix, r, t in PREFIX_RULES:
        if heading.startswith(prefix):
            role, track = r, t
            break
    active = 1
    if "梯队专用" in heading or domain == "ai_safety":
        active = 0
    if domain == "ai_safety":
        role = "面试素材"
    return role, track, active


def _split_segments(lines: list[str]) -> list[dict]:
    """把一题的内容切成带标签的小节：#### 小标题 / 加粗标签行 / blockquote。"""
    segments: list[dict] = []

    def new_seg(label: str, target: str | None, first: str) -> dict:
        seg = {"label": label, "target": target, "kind": "plain",
               "lines": [first] if first else []}
        segments.append(seg)
        return seg

    cur = new_seg("__preamble__", None, "")
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            if cur["kind"] == "quote":
                cur = new_seg("__after_quote__", "reviewer", "")
            continue
        sub = SUBHEADING_RE.match(stripped)
        if sub:
            label = sub.group(1)
            cur = new_seg(label, _resolve_label(label), "")
            cur["kind"] = "heading"
            continue
        if stripped.startswith(">"):
            if cur["kind"] != "quote":
                cur = new_seg("blockquote", "reviewer", "")
                cur["kind"] = "quote"
            cur["lines"].append(re.sub(r"^\s*>\s?", "", line))
            continue
        bold = BOLD_RE.match(stripped)
        if bold:
            label = bold.group(1)
            target = _resolve_label(label)
            if target is not None:
                cur = new_seg(label, target, line)
                cur["kind"] = "bold"
                continue
        if cur["kind"] == "quote":
            cur = new_seg("__after_quote__", "reviewer", "")
        cur["lines"].append(line)
    return segments


def _segment_text(seg: dict) -> str:
    # heading 小节在 new_seg 时即未存入标题行，无需再跳过首行
    text = "\n".join(seg["lines"]).strip()
    if seg["kind"] == "bold":
        # 去掉行首的 **标签** 标记及其紧随的冒号/破折号
        text = re.sub(r"^\*\*[^*]+\*\*\s*[：:]?\s*", "", text).strip()
    return text


def parse_question(heading: str, domain: str, lines: list[str]) -> tuple[dict | None, list[str]]:
    """解析单个题目，返回 (question|None, anomalies)。"""
    anomalies: list[str] = []
    m = BANK_ID_RE.search(heading)
    if not m:
        return None, [f"标题无法提取 bank_id：{heading}"]
    bank_id = m.group(0)
    if "：" in heading:
        title = heading.split("：", 1)[1].strip()
    else:
        title = heading.strip()
        anomalies.append(f"[{bank_id}] 标题缺少全角冒号，title 取整个标题")
    role, track, active = _classify_heading(heading, domain)

    fields: dict[str, list[str]] = {k: [] for k in ("body", "reviewer") + META_KEYS}
    for seg in _split_segments(lines):
        text = _segment_text(seg)
        if not text:
            continue
        if seg["target"] is None:
            anomalies.append(f"[{bank_id}] 题面区外存在未识别的散落文本，已并入 reviewer_notes："
                             f"{text[:60].replace(chr(10), ' ')}…")
            fields["reviewer_notes"].append(text)
            continue
        if seg["target"] == "body" and seg["kind"] == "bold" and seg["label"].startswith("配套材料"):
            text = "\n".join(seg["lines"]).strip()  # 配套材料保留原样（含标签行）
        fields[seg["target"]].append(text)

    meta = {k: "\n\n".join(fields[k]).strip() for k in META_KEYS if k != "reviewer_notes"}
    meta["reviewer_notes"] = "\n\n".join(fields["reviewer"]).strip()
    body = "\n\n".join(fields["body"]).strip()
    if not body:
        anomalies.append(f"[{bank_id}] 未解析到题面 body")
    for word in LEAK_WORDS:
        if word in body:
            anomalies.append(f"[{bank_id}] 题面 body 混入评审字样「{word}」")
    q = {
        "bank_id": bank_id,
        "domain": domain,
        "track": track,
        "role": role,
        "active": active,
        "title": title,
        "body": body,
        "meta": meta,
    }
    return q, anomalies


def parse_bank(md_path: str | Path) -> tuple[list[dict], dict]:
    """解析题库 md，返回 (questions, report)。报告含统计、跳过的附录与异常。"""
    lines = Path(md_path).read_text(encoding="utf-8").splitlines()
    questions: list[dict] = []
    anomalies: list[str] = []
    skipped_appendices: list[str] = []

    domain = None
    cur_heading: str | None = None
    cur_domain: str | None = None
    cur_lines: list[str] = []

    def flush_question():
        nonlocal cur_heading, cur_lines
        if cur_heading is None:
            return
        q, ans = parse_question(cur_heading, cur_domain or "", cur_lines)
        anomalies.extend(ans)
        if q is not None:
            questions.append(q)
        cur_heading = None
        cur_lines = []

    for raw in lines:
        h2 = re.match(r"^##\s+(.+?)\s*$", raw)
        h3 = re.match(r"^###\s+(.+?)\s*$", raw)
        if h2:
            flush_question()
            cur_heading, cur_lines = None, []
            title = h2.group(1)
            if "附三" in title:  # 判断顺序：附三标题里含「领域九」，须先于领域映射
                domain = "ai_safety"
            elif "附一" in title or "附二" in title:
                domain = None
                skipped_appendices.append(f"## {title}")
            else:
                for key, dom in DOMAIN_MAP.items():
                    if key in title:
                        domain = dom
                        break
            continue
        if h3:
            flush_question()
            title = h3.group(1)
            if title.startswith("附"):
                skipped_appendices.append(f"### {title}（domain={domain}）")
                cur_heading, cur_lines = None, []
                continue
            if domain is None:
                skipped_appendices.append(f"### {title}（位于非领域章节，跳过）")
                cur_heading, cur_lines = None, []
                continue
            cur_heading, cur_domain, cur_lines = title, domain, []
            continue
        if cur_heading is not None:
            cur_lines.append(raw)
    flush_question()

    seen: set[str] = set()
    for q in questions:
        if q["bank_id"] in seen:
            anomalies.append(f"bank_id 重复：{q['bank_id']}")
        seen.add(q["bank_id"])

    report = {
        "total": len(questions),
        "questions": questions,
        "anomalies": anomalies,
        "skipped_appendices": skipped_appendices,
    }
    return questions, report


def upsert_all(conn, questions: list[dict]) -> dict:
    """按 bank_id 幂等 upsert，返回 {inserted, updated, ids}。"""
    inserted, updated = 0, 0
    for q in questions:
        now = _now_iso()
        meta_json = json.dumps(q["meta"], ensure_ascii=False)
        row = conn.execute(
            "SELECT id, created_at FROM question WHERE bank_id = ?", (q["bank_id"],)
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE question SET domain=?, track=?, title=?, body=?, tags=?,"
                " active=?, role=?, meta=?, updated_at=? WHERE bank_id=?",
                (q["domain"], q["track"], q["title"], q["body"], "[]",
                 q["active"], q["role"], meta_json, now, q["bank_id"]),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO question (id, domain, track, title, body, tags, active,"
                " bank_id, role, meta, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (q["bank_id"], q["domain"], q["track"], q["title"], q["body"], "[]",
                 q["active"], q["bank_id"], q["role"], meta_json, now, now),
            )
            inserted += 1
    return {"inserted": inserted, "updated": updated}


def format_stats(questions: list[dict]) -> str:
    stats: dict[str, dict] = {}
    for q in questions:
        d = stats.setdefault(q["domain"], {"total": 0, "tracks": {}, "active1": 0, "active0": 0,
                                           "roles": {}})
        d["total"] += 1
        d["tracks"][q["track"]] = d["tracks"].get(q["track"], 0) + 1
        d["active1" if q["active"] else "active0"] += 1
        d["roles"][q["role"]] = d["roles"].get(q["role"], 0) + 1
    lines = ["domain       total  track分布                active=1/0  role分布"]
    for dom, d in sorted(stats.items()):
        tracks = ",".join(f"{t}:{n}" for t, n in sorted(d["tracks"].items()))
        roles = ",".join(f"{r}:{n}" for r, n in sorted(d["roles"].items()))
        lines.append(f"{dom:<12} {d['total']:>3}   {tracks:<24} {d['active1']}/{d['active0']}"
                     f"        {roles}")
    lines.append(f"合计 {sum(d['total'] for d in stats.values())} 题")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="题库 v5 解析导入")
    ap.add_argument("--md", default=str(DEFAULT_MD), help="题库 md 路径")
    ap.add_argument("--db", default=None, help="SQLite 文件路径（默认 app.config.DB_PATH）")
    ap.add_argument("--dry-run", action="store_true", help="只解析统计，不写库")
    args = ap.parse_args(argv)

    if args.db:
        os.environ["ALE_THIC_DB_PATH"] = str(Path(args.db).resolve())
    sys.path.insert(0, str(SERVER_DIR))
    from app import db

    questions, report = parse_bank(args.md)
    print(format_stats(questions))
    print(f"\n跳过的非题内容（{len(report['skipped_appendices'])} 处）：")
    for s in report["skipped_appendices"]:
        print(f"  - {s}")
    if report["anomalies"]:
        print(f"\n格式异常（{len(report['anomalies'])} 条）：")
        for s in report["anomalies"]:
            print(f"  ! {s}")
    else:
        print("\n格式异常：无")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    db.init_db()
    with db.connection() as conn:
        result = upsert_all(conn, questions)
    print(f"\n导入完成：新增 {result['inserted']} 条，更新 {result['updated']} 条"
          f"（db={db.config.DB_PATH}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
