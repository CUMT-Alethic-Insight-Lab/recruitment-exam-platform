import json
import sqlite3

import pytest

from app import config, db, maintenance
from scripts.sync_membership import main, plan_sync, read_members, snapshot, sync_connection


@pytest.fixture
def member_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "members.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    with db.connection() as conn:
        conn.execute("INSERT INTO campaign (id,name,year,status,expires_at,created_at) VALUES ('c','test',2026,'closed','2026-11-15T16:00:00+00:00','now')")
        for sid, name, status in (("00000001", "Member", "rejected"),
                                  ("00000002", "Other", "interviewed"),
                                  ("00000003", "Prior", "tested")):
            conn.execute("INSERT INTO account (student_id,role,nickname,pw_salt,pw_hash,pw_changed,created_at,updated_at) VALUES (?,'interviewer',?,'salt','hash',1,'then','then')", (sid, name))
            conn.execute("INSERT INTO application (student_id,campaign_id,name,gender,class_name,qq,lab_intent,domains,focus_next_year,status,created_at,updated_at) VALUES (?,'c',?,'','','',0,'[]','',?,'then','then')", (sid, name, status))
        conn.execute("INSERT INTO membership (student_id,name,source,created_at) VALUES ('00000003','Prior','previous','then')")
    return tmp_path


def test_sync_dry_run_apply_idempotent(member_db):
    members = [{"name": "Member", "student_id": "00000001"}]
    with db.connection() as conn:
        before = snapshot(conn)
        report = sync_connection(conn, members, "c")
        assert snapshot(conn) == before
        assert report["matched_application_count"] == 2
        assert report["account_changes"] == report["deletes"] == []
        assert {r["student_id"]: r["after"] for r in report["status_changes"]} == {
            "00000001": "admitted", "00000002": "rejected", "00000003": "admitted"}
        conn.commit()
        result = sync_connection(conn, members, "c", apply=True)
        assert result["mode"] == "applied"
        after = snapshot(conn)
        assert after["account"] == before["account"]
        assert after["campaign"] == before["campaign"]
        assert len(after["membership"]) == 2
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 4
        previous = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit_log WHERE action='membership_status_sync'")]
        assert {r["before"] for r in previous} == {"rejected", "interviewed", "tested"}
        assert sync_connection(conn, members, "c", apply=True)["status_changes"] == []
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 4


def test_unresolved_conflicts_block_apply(member_db):
    with db.connection() as conn:
        before = snapshot(conn)
        members = [{"name": "Member", "student_id": None}]
        report = sync_connection(conn, members, "c")
        assert report["apply_blocked"]
        assert report["held_applications"][0]["student_id"] == "00000001"
        with pytest.raises(ValueError, match="apply blocked"):
            sync_connection(conn, members, "c", apply=True)
        assert snapshot(conn) == before
        conflicting = [{"name": "Member", "student_id": "00000002"}]
        assert plan_sync(before, conflicting, "c")["identity_conflicts"]
        with pytest.raises(ValueError, match="apply blocked"):
            sync_connection(conn, conflicting, "c", apply=True)


def test_apply_rolls_back_if_audit_fails(member_db):
    with db.connection() as conn:
        conn.execute("DROP TABLE audit_log")
        conn.commit()
        before = snapshot(conn)
        with pytest.raises(sqlite3.OperationalError):
            sync_connection(conn, [{"name": "Member", "student_id": "00000001"}], "c", apply=True)
        assert snapshot(conn) == before


def test_cli_readonly_dump_and_database(member_db, tmp_path):
    members = tmp_path / "roster.json"
    members.write_text('[{"name":"Member","student_id":"00000001"}]', encoding="utf-8")
    report = tmp_path / "report.json"
    before = config.DB_PATH.read_bytes()
    assert main(["--db", str(config.DB_PATH), "--members", str(members), "--campaign", "c", "--report", str(report)]) == 0
    assert config.DB_PATH.read_bytes() == before
    with db.connection() as conn:
        dump = snapshot(conn)
    dump_path = tmp_path / "dump.json"
    dump_path.write_text(json.dumps(dump), encoding="utf-8")
    assert main(["--dump", str(dump_path), "--members", str(members), "--campaign", "c", "--report", str(report)]) == 0
    assert json.loads(report.read_text())["mode"] == "dry-run"
    with pytest.raises(SystemExit):
        main(["--dump", str(dump_path), "--members", str(members), "--campaign", "c", "--report", str(report), "--apply"])
    members.write_text('[{"name":"Member","student_id":1}]', encoding="utf-8")
    with pytest.raises(ValueError):
        read_members(members)


def test_purge_retains_accounts_memberships_and_cleans_dependencies(member_db):
    with db.connection() as conn:
        accounts = snapshot(conn)["account"]
        members = snapshot(conn)["membership"]
        conn.execute("INSERT INTO exam_session (id,campaign_id,student_id,session_token,machine_id,track,opened_at) VALUES ('s','c','00000003','token','machine','A','then')")
        conn.execute("INSERT INTO exam_score (session_id,created_at,updated_at) VALUES ('s','then','then')")
        conn.execute("INSERT INTO interview_score (campaign_id,student_id,created_at,updated_at) VALUES ('c','00000003','then','then')")
        conn.execute("INSERT INTO interview_slot (id,campaign_id,start_at,end_at,booked_by,created_at) VALUES ('slot','c','start','end','00000003','then')")
        conn.execute("INSERT INTO login_session VALUES ('token','applicant','c','00000003','then','later')")
        conn.execute("INSERT INTO login_session VALUES ('global','interviewer',NULL,'00000003','then','later')")
    assert maintenance.purge_expired("2026-10-23T00:00:00+00:00")["purged"] == []
    assert maintenance.purge_expired("2026-11-16T00:00:00+00:00")["purged"] == ["c"]
    with db.connection() as conn:
        assert snapshot(conn)["account"] == accounts
        assert snapshot(conn)["membership"] == members
        for table in ("application", "exam_session", "exam_score", "interview_score", "interview_slot"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT token_hash FROM login_session").fetchone()[0] == "global"
        assert conn.execute("SELECT expires_at FROM campaign").fetchone()[0] == "2026-11-15T16:00:00+00:00"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert maintenance.purge_expired("2026-11-16T00:00:00+00:00")["purged"] == []
