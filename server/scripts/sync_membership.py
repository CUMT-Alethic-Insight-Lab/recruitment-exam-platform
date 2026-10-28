#!/usr/bin/env python3
"""Sync a reviewed roster without changing account roles, credentials or deleting rows.

Default: --dump snapshot.json --members reviewed.json --campaign ID --report plan.json
Local DB dry-run: replace --dump with --db path.sqlite. Only --db ... --apply writes.
Members JSON: [{"name": "...", "student_id": "00000000"}, ...]. Missing IDs are
reported and block apply. Existing memberships are retained even if absent in input.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import MEMBERSHIP_SCHEMA


def read_members(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("members must be a nonempty reviewed JSON array")
    members = []
    identities = {}
    names = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each member must be a JSON object")
        name = str(row.get("name") or "").strip()
        sid = row.get("student_id")
        if not name or (sid is not None and (
                not isinstance(sid, str) or len(sid) != 8 or not sid.isascii() or not sid.isdigit())):
            raise ValueError("each member needs a name and an eight-digit string ID or null")
        if sid and sid in identities and identities[sid] != name:
            raise ValueError(f"conflicting names for student_id {sid}")
        if name in names and names[name] != sid:
            raise ValueError(f"conflicting IDs for member {name}")
        names[name] = sid
        if sid:
            identities[sid] = name
        member = {"name": name, "student_id": sid}
        if member not in members:
            members.append(member)
    return members


def snapshot(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            if table in tables else []
            for table in ("application", "account", "campaign", "membership")}


def plan_sync(data, members, campaign):
    if not any(c["id"] == campaign for c in data.get("campaign", [])):
        raise ValueError("campaign not found; use its exact ID")
    existing = {r["student_id"] for r in data.get("membership", [])}
    supplied = {r["student_id"] for r in members if r["student_id"]}
    protected = existing | supplied
    accounts = {r["student_id"] for r in data.get("account", [])}
    unresolved = [r for r in members if not r["student_id"]]
    unresolved_names = {r["name"] for r in unresolved}
    apps = [r for r in data.get("application", []) if r["campaign_id"] == campaign]
    conflicts = []
    for member in members:
        for app in apps:
            if member["student_id"] and app["name"] == member["name"] and app["student_id"] != member["student_id"]:
                conflicts.append({"member": member, "application_student_id": app["student_id"]})
            elif member["student_id"] == app["student_id"] and app["name"] != member["name"]:
                conflicts.append({"member": member, "application_name": app["name"]})
    changes = []
    matched = []
    held = []
    for app in apps:
        sid = app["student_id"]
        if sid not in protected and app["name"] in unresolved_names:
            held.append({"student_id": sid, "name": app["name"], "status": app["status"]})
            continue
        target = "admitted" if sid in protected else "rejected"
        if sid in protected:
            matched.append({"student_id": sid, "name": app["name"], "previous_status": app["status"]})
        if app["status"] != target:
            changes.append({"student_id": sid, "name": app["name"], "before": app["status"], "after": target})
    additions = [r for r in members if r["student_id"] and r["student_id"] not in existing]
    return {
        "mode": "dry-run", "campaign_id": campaign,
        "roster_count": len(members), "protected_id_count": len(protected),
        "existing_membership_count": len(existing), "application_count": len(apps),
        "matched_application_count": len(matched), "matched_applications": matched,
        "membership_additions": additions, "status_changes": changes,
        "unresolved_members": unresolved, "identity_conflicts": conflicts,
        "held_applications": held,
        "members_without_account": [r for r in members if r["student_id"] not in accounts],
        "protected_without_application": sorted(protected - {r["student_id"] for r in apps}),
        "account_changes": [], "deletes": [],
        "review_preservation": "Previous statuses archived in audit_log on apply; scores and sessions unchanged.",
        "retention": "All account rows and existing memberships retained; campaign expires_at unchanged.",
        "apply_blocked": bool(unresolved or conflicts),
    }


def sync_connection(conn, members, campaign, apply=False, source="reviewed-roster"):
    if apply:
        conn.execute("BEGIN IMMEDIATE")
    report = plan_sync(snapshot(conn), members, campaign)
    if not apply:
        return report
    if report["apply_blocked"]:
        conn.rollback()
        raise ValueError("unresolved IDs or identity conflicts: apply blocked; review dry-run report")
    try:
        now = datetime.now(timezone.utc).isoformat()
        # One statement: executescript would implicitly commit the transaction.
        conn.execute(MEMBERSHIP_SCHEMA.strip())
        for row in report["membership_additions"]:
            conn.execute("INSERT INTO membership (student_id, name, source, created_at) VALUES (?, ?, ?, ?)",
                         (row["student_id"], row["name"], source, now))
        for row in report["status_changes"]:
            result = conn.execute(
                "UPDATE application SET status=?, updated_at=? WHERE campaign_id=? AND student_id=? AND status=?",
                (row["after"], now, campaign, row["student_id"], row["before"]))
            if result.rowcount != 1:
                raise ValueError("application changed during sync")
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                ("membership-sync", "membership_status_sync", f"{campaign}/{row['student_id']}",
                 json.dumps(row, ensure_ascii=False), now))
        for row in report["membership_additions"]:
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                ("membership-sync", "membership_add", row["student_id"],
                 json.dumps({**row, "source": source}, ensure_ascii=False), now))
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("foreign key check failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    report["mode"] = "applied"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--dump", type=Path)
    inputs.add_argument("--db", type=Path)
    parser.add_argument("--members", type=Path, required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and not args.db:
        parser.error("--apply requires --db; dump input is read-only")
    if args.report.resolve() in {p.resolve() for p in (args.dump, args.db, args.members) if p}:
        parser.error("report cannot overwrite an input")
    members = read_members(args.members)
    if args.dump:
        report = plan_sync(json.loads(args.dump.read_text(encoding="utf-8")), members, args.campaign)
    else:
        mode = "rw" if args.apply else "ro"
        with sqlite3.connect(args.db.resolve().as_uri() + f"?mode={mode}", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            report = sync_connection(conn, members, args.campaign, args.apply, args.members.name)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("mode", "roster_count", "matched_application_count", "apply_blocked")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
