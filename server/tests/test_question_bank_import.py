"""题库 v5 解析导入测试：解析计数、track/active 口径、题面防泄漏、upsert 幂等。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_question_bank import parse_bank, upsert_all  # noqa: E402

from app import db  # noqa: E402

MD_PATH = Path(__file__).resolve().parent.parent / "data" / "question_bank_v5.md"

EXPECTED_DOMAIN_COUNTS = {
    "llm_arch": 5,
    "cv_arch": 3,
    "vlm_align": 3,
    "rl": 5,
    "agent": 4,
    "memory": 3,
    "infra": 4,
    "ai_safety": 3,
}
EXPECTED_BANK_IDS = {
    "L1", "L2", "L3", "L4", "L5", "C1", "C2", "C3",
    "V1", "V2", "V3",
    "R1", "R2", "R3", "R5", "R4",
    "A1", "A2", "A3", "A4",
    "M1", "M2", "M3",
    "I1", "I2", "I3", "I4",
    "S1", "S2", "S3",
}
LEAK_WORDS = (
    "评审专用", "出题人备注", "翻车模式", "优秀回答", "不下发",
    "评分参考", "评审抓手", "面试追问弹药", "锚点清单",
)

# L2 源文件本就没有出题人备注节，按契约置空串
QUESTIONS_WITHOUT_NOTES = {"L2"}


@pytest.fixture(scope="module")
def parsed():
    questions, report = parse_bank(MD_PATH)
    return questions, report


def _by_id(questions):
    return {q["bank_id"]: q for q in questions}


def test_parse_total_and_domain_counts(parsed):
    questions, report = parse_bank(MD_PATH)
    assert report["total"] == 30
    assert {q["bank_id"] for q in questions} == EXPECTED_BANK_IDS
    counts: dict[str, int] = {}
    for q in questions:
        counts[q["domain"]] = counts.get(q["domain"], 0) + 1
    assert counts == EXPECTED_DOMAIN_COUNTS
    # 领域/track/active 分布（I4 加试默认下发；L4/L5 开放题入 track3，active=26/4）
    tracks: dict[str, int] = {}
    active = {1: 0, 0: 0}
    for q in questions:
        tracks[q["track"]] = tracks.get(q["track"], 0) + 1
        active[q["active"]] += 1
    assert tracks == {"track1": 19, "track2": 7, "track3": 4}
    assert active == {1: 26, 0: 4}
    assert report["anomalies"] == []


def test_body_clean_no_review_leak(parsed):
    questions, _ = parsed
    for q in questions:
        assert q["body"].strip(), f"{q['bank_id']} 题面为空"
        for word in LEAK_WORDS:
            assert word not in q["body"], f"{q['bank_id']} 题面泄漏「{word}」"


def test_meta_fields_present(parsed):
    questions, _ = parsed
    for q in questions:
        assert set(q["meta"].keys()) == {
            "basis", "test_points", "pass_signals", "failure_modes", "reviewer_notes",
        }
        for key in ("basis", "test_points", "pass_signals", "failure_modes"):
            assert q["meta"][key].strip(), f"{q['bank_id']} meta.{key} 为空"
        if q["bank_id"] not in QUESTIONS_WITHOUT_NOTES:
            assert q["meta"]["reviewer_notes"].strip(), f"{q['bank_id']} reviewer_notes 为空"
        json.dumps(q["meta"], ensure_ascii=False)  # meta 必须可 JSON 序列化


def test_track_role_active_mapping(parsed):
    questions, _ = parsed
    qs = _by_id(questions)
    # 加试 I4 → active=1（默认下发）；梯队专用 R5 / 附三面试素材 → active=0
    assert qs["I4"]["role"] == "加试" and qs["I4"]["active"] == 1
    assert qs["R5"]["role"] == "备选" and qs["R5"]["active"] == 0
    for sid in ("S1", "S2", "S3"):
        assert qs[sid]["domain"] == "ai_safety"
        assert qs[sid]["role"] == "面试素材"
        assert qs[sid]["active"] == 0
    # 常规机试题 active=1
    for qid, role, track in [
        ("L1", "正选", "track1"), ("L2", "备选", "track1"), ("L3", "开放", "track3"),
        ("L4", "开放", "track3"), ("L5", "开放", "track3"),
        ("C3", "实操", "track2"), ("R4", "实操", "track2"), ("A3", "实操", "track2"),
        ("A4", "实操", "track2"), ("I3", "实操", "track2"), ("M3", "实操", "track2"),
        ("V3", "开放", "track3"), ("I1", "正选", "track1"), ("M2", "备选", "track1"),
    ]:
        assert qs[qid]["role"] == role, qid
        assert qs[qid]["track"] == track, qid
        assert qs[qid]["active"] == 1, qid


def test_appendix_sections_skipped(parsed):
    _, report = parsed
    assert len(report["skipped_appendices"]) == 7
    assert any("附一" in s for s in report["skipped_appendices"])
    assert any("附二" in s for s in report["skipped_appendices"])
    # 各领域文末「附：事实锚点核验清单」均为非题内容
    assert sum(1 for s in report["skipped_appendices"] if s.startswith("### 附")) == 5
    # 锚点清单内容不进任何题的 body
    questions, _ = parsed
    for q in questions:
        assert "核验状态" not in q["body"] and "置信" not in q["body"]


def test_upsert_idempotent(parsed):
    questions, _ = parsed
    bank_ids = [q["bank_id"] for q in questions]

    db.init_db()
    with db.connection() as conn:
        conn.execute(
            f"DELETE FROM question WHERE bank_id IN ({','.join('?' * len(bank_ids))})",
            bank_ids,
        )

    with db.connection() as conn:
        first = upsert_all(conn, questions)
    assert first == {"inserted": 30, "updated": 0}

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM question WHERE bank_id IS NOT NULL"
        ).fetchall()
        snapshot = {
            r["bank_id"]: (r["id"], r["domain"], r["track"], r["title"], r["body"],
                           r["role"], r["meta"], r["active"], r["created_at"])
            for r in rows
        }
    assert len(rows) == 30

    with db.connection() as conn:
        second = upsert_all(conn, questions)
    assert second == {"inserted": 0, "updated": 30}

    with db.connection() as conn:
        rows2 = conn.execute(
            "SELECT * FROM question WHERE bank_id IS NOT NULL"
        ).fetchall()
        snapshot2 = {
            r["bank_id"]: (r["id"], r["domain"], r["track"], r["title"], r["body"],
                           r["role"], r["meta"], r["active"], r["created_at"])
            for r in rows2
        }
    # 两遍导入后行数不变，内容字段一致，id/created_at 未被改动
    assert len(rows2) == 30
    assert snapshot2 == snapshot

    # meta 落库为合法 JSON 且 body 与解析结果一致
    by_id = _by_id(questions)
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM question WHERE bank_id = ?", ("L1",)
        ).fetchone()
    assert row["body"] == by_id["L1"]["body"]
    assert json.loads(row["meta"]) == by_id["L1"]["meta"]
    assert row["role"] == "正选" and row["active"] == 1
