"""题库解析导入测试：解析计数、track/active 口径、题面防泄漏、upsert 幂等。

使用 tests/fixtures/question_bank_sample.md 合成夹具（覆盖全部标题前缀口径、
meta 字段抽取、评审节隔离、附录跳过）。真实题库的解析校验在 org 私有仓
question-bank 中进行，本仓只验证解析器契约。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_question_bank import parse_bank, upsert_all  # noqa: E402

from app import db  # noqa: E402

MD_PATH = Path(__file__).resolve().parent / "fixtures" / "question_bank_sample.md"

EXPECTED_DOMAIN_COUNTS = {
    "llm_arch": 2,
    "rl": 3,
    "ai_safety": 1,
}
EXPECTED_BANK_IDS = {"L1", "L2", "R1", "R2", "R3", "S1"}
LEAK_WORDS = (
    "评审专用", "出题人备注", "翻车模式", "优秀回答", "不下发",
    "评分参考", "评审抓手", "面试追问弹药", "锚点清单",
)

# L2 夹具中没有评审小节，按契约 reviewer_notes 置空串
QUESTIONS_WITHOUT_NOTES = {"L2"}


@pytest.fixture(scope="module")
def parsed():
    questions, report = parse_bank(MD_PATH)
    return questions, report


def _by_id(questions):
    return {q["bank_id"]: q for q in questions}


def test_parse_total_and_domain_counts(parsed):
    questions, report = parsed
    assert report["total"] == 6
    assert {q["bank_id"] for q in questions} == EXPECTED_BANK_IDS
    counts: dict[str, int] = {}
    for q in questions:
        counts[q["domain"]] = counts.get(q["domain"], 0) + 1
    assert counts == EXPECTED_DOMAIN_COUNTS
    tracks: dict[str, int] = {}
    active = {1: 0, 0: 0}
    for q in questions:
        tracks[q["track"]] = tracks.get(q["track"], 0) + 1
        active[q["active"]] += 1
    assert tracks == {"track1": 5, "track2": 1}
    assert active == {1: 4, 0: 2}
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
    # 加试 R3 → active=1（默认下发）；梯队专用 R2 → active=0
    assert qs["R3"]["role"] == "加试" and qs["R3"]["active"] == 1
    assert qs["R2"]["role"] == "备选" and qs["R2"]["active"] == 0
    # 附三（AI 安全）整域为面试素材，active=0
    assert qs["S1"]["domain"] == "ai_safety"
    assert qs["S1"]["role"] == "面试素材"
    assert qs["S1"]["active"] == 0
    # 常规机试题 active=1
    for qid, role, track in [
        ("L1", "正选", "track1"), ("L2", "备选", "track1"),
        ("R1", "实操", "track2"),
    ]:
        assert qs[qid]["role"] == role, qid
        assert qs[qid]["track"] == track, qid
        assert qs[qid]["active"] == 1, qid


def test_appendix_sections_skipped(parsed):
    _, report = parsed
    assert len(report["skipped_appendices"]) == 4
    assert any("附一" in s for s in report["skipped_appendices"])
    assert any("附二" in s for s in report["skipped_appendices"])
    # 领域文末「附：事实锚点核验清单」为非题内容
    assert sum(1 for s in report["skipped_appendices"] if s.startswith("### 附")) == 2
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
    assert first == {"inserted": 6, "updated": 0}

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM question WHERE bank_id IS NOT NULL"
        ).fetchall()
        snapshot = {
            r["bank_id"]: (r["id"], r["domain"], r["track"], r["title"], r["body"],
                           r["role"], r["meta"], r["active"], r["created_at"])
            for r in rows
        }
    assert len(rows) == 6

    with db.connection() as conn:
        second = upsert_all(conn, questions)
    assert second == {"inserted": 0, "updated": 6}

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
    assert len(rows2) == 6
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
