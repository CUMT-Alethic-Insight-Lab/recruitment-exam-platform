import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from . import db
from .org_auth import OrgIdentity

KIND = "project-join"
FORM_REVISION = "1"
FORM_UPDATED_AT = "2026-10-27T00:00:00+00:00"
TRANSITIONS = {
    "draft": frozenset({"submitted"}),
    "submitted": frozenset({"in_review", "needs_information", "approved", "rejected", "withdrawn"}),
    "in_review": frozenset({"needs_information", "approved", "rejected", "withdrawn"}),
    "needs_information": frozenset({"submitted", "withdrawn"}),
    "approved": frozenset(),
    "rejected": frozenset(),
    "withdrawn": frozenset(),
}


def utcnow():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def schema():
    return {
        "kind": KIND,
        "revision": FORM_REVISION,
        "source_ref": "form://project-join/1",
        "updated_at": FORM_UPDATED_AT,
        "fields": [
            {"name": "project_id", "type": "string", "required": True},
            {"name": "motivation", "type": "string", "required": True, "min_length": 10, "max_length": 2000},
            {"name": "weekly_hours", "type": "integer", "required": True, "minimum": 1, "maximum": 40},
            {"name": "notes", "type": "string", "required": False, "max_length": 2000},
        ],
        "additional_fields": False,
    }


def _fail(status, message):
    raise HTTPException(status_code=status, detail=message)


def _project(conn, project_id):
    row = conn.execute(
        "SELECT id, recruiting FROM org_project WHERE id = ?"
        " AND visibility IN ('public', 'member')", (project_id,),
    ).fetchone()
    if row is None:
        _fail(404, "Project not found")
    if not row["recruiting"]:
        _fail(409, "Project is not recruiting")


def normalize(conn, kind, form_revision, fields):
    if kind != KIND or form_revision != FORM_REVISION:
        _fail(409, "Unsupported form kind or revision; refresh the form schema")
    if not isinstance(fields, dict) or set(fields) - {"project_id", "motivation", "weekly_hours", "notes"}:
        _fail(422, "Only the fields in the current project-join schema are accepted")
    result = {}
    for key, minimum, maximum in (("project_id", 1, 200), ("motivation", 10, 2000), ("notes", 0, 2000)):
        value = fields.get(key, "")
        if not isinstance(value, str):
            _fail(422, f"{key} must be a string")
        value = value.strip().replace("\r\n", "\n").replace("\r", "\n")
        if not minimum <= len(value) <= maximum:
            _fail(422, f"Invalid {key} length")
        result[key] = value
    hours = fields.get("weekly_hours")
    if type(hours) is not int or not 1 <= hours <= 40:
        _fail(422, "weekly_hours must be an integer between 1 and 40")
    result["weekly_hours"] = hours
    _project(conn, result["project_id"])
    preview = {"kind": kind, "form_revision": form_revision, "fields": result}
    return preview, digest(preview)


def validate(kind, form_revision, fields):
    with db.connection() as conn:
        preview, content_hash = normalize(conn, kind, form_revision, fields)
    return {"valid": True, "preview": preview, "content_hash": content_hash}


def _owned(conn, request_id, identity):
    row = conn.execute(
        "SELECT * FROM service_request WHERE id = ? AND applicant_id = ?",
        (request_id, identity.principal_id),
    ).fetchone()
    if row is None:
        _fail(404, "Application not found")
    return row


def _preview(row):
    return {"kind": row["kind"], "form_revision": row["form_revision"], "fields": json.loads(row["payload_json"])}


def _view(row):
    return {
        "id": row["id"], "kind": row["kind"], "form_revision": row["form_revision"],
        "project_id": row["project_id"], "status": row["status"], "preview": _preview(row),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "submitted_at": row["submitted_at"],
    }


def save_draft(identity: OrgIdentity, kind, form_revision, fields, draft_id=None):
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        preview, content_hash = normalize(conn, kind, form_revision, fields)
        now = utcnow().isoformat()
        if draft_id:
            row = _owned(conn, draft_id, identity)
            if row["status"] not in ("draft", "needs_information"):
                _fail(409, "Application cannot be edited in this state")
            conn.execute(
                "UPDATE service_request SET project_id = ?, kind = ?, form_revision = ?,"
                " payload_json = ?, content_hash = ?, updated_at = ? WHERE id = ?",
                (preview["fields"]["project_id"], kind, form_revision, canonical(preview["fields"]), content_hash, now, draft_id),
            )
            conn.execute("DELETE FROM org_confirmation WHERE request_id = ?", (draft_id,))
        else:
            draft_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO service_request (id, applicant_id, project_id, kind, form_revision,"
                " payload_json, content_hash, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                (draft_id, identity.principal_id, preview["fields"]["project_id"], kind, form_revision,
                 canonical(preview["fields"]), content_hash, now, now),
            )
        return _view(_owned(conn, draft_id, identity))


def list_mine(identity, limit, cursor, project_id=None):
    project_clause = " AND project_id = ?" if project_id else ""
    params = [identity.principal_id, cursor or ""]
    if project_id:
        params.append(project_id)
    params.append(limit + 1)
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM service_request WHERE applicant_id = ? AND id > ?"
            + project_clause + " ORDER BY id LIMIT ?", params,
        ).fetchall()
    return {"items": [_view(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}


def list_review_queue(identity, limit, cursor):
    if identity.auth_type != "web" or identity.role != "admin":
        _fail(403, "Web administrator required")
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM service_request WHERE status IN ('submitted', 'in_review', 'needs_information')"
            " AND id > ? ORDER BY id LIMIT ?", (cursor or "", limit + 1),
        ).fetchall()
    return {"items": [_view(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}


def detail(identity, request_id):
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM service_request WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            _fail(404, "Application not found")
        if row["applicant_id"] != identity.principal_id:
            if (identity.auth_type != "web" or identity.role != "admin"
                    or row["submitted_at"] is None):
                _fail(404, "Application not found")
        result = _view(row)
        reviews = conn.execute(
            "SELECT decision, reason, reviewer_id, decided_at FROM service_request_review"
            " WHERE request_id = ? ORDER BY decided_at, id", (request_id,),
        ).fetchall()
        result["reviews"] = [dict(review) for review in reviews]
    return result


def issue_confirmation(identity, request_id):
    if identity.auth_type != "web":
        _fail(403, "Trusted web confirmation required")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _owned(conn, request_id, identity)
        if row["status"] not in ("draft", "needs_information"):
            _fail(409, "Application cannot be confirmed in this state")
        preview, content_hash = normalize(conn, row["kind"], row["form_revision"], json.loads(row["payload_json"]))
        if row["content_hash"] != content_hash or row["project_id"] != preview["fields"]["project_id"]:
            _fail(409, "Draft content has changed; save the draft again")
        token = secrets.token_urlsafe(32)
        now = utcnow()
        expires_at = (now + timedelta(minutes=5)).isoformat()
        conn.execute("DELETE FROM org_confirmation WHERE request_id = ?", (request_id,))
        conn.execute(
            "INSERT INTO org_confirmation (token_hash, principal_id, request_id, content_hash, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(), identity.principal_id, request_id, content_hash, expires_at),
        )
    return {"confirmation": token, "expires_at": expires_at, "preview": _preview(row)}


def submit(identity, draft_id, confirmation, idempotency_key):
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 200:
        _fail(422, "A non-empty Idempotency-Key of at most 200 characters is required")
    request_hash = digest({"draft_id": draft_id, "confirmation": confirmation})
    operation = "applications:submit"
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT request_hash, response_json FROM org_idempotency WHERE principal_id = ?"
            " AND operation = ? AND key = ?",
            (identity.principal_id, operation, idempotency_key),
        ).fetchone()
        if previous:
            if previous["request_hash"] != request_hash:
                _fail(409, "Idempotency-Key was already used for a different request")
            return json.loads(previous["response_json"])
        row = _owned(conn, draft_id, identity)
        if "submitted" not in TRANSITIONS.get(row["status"], ()):
            _fail(409, "Application cannot be submitted in this state")
        preview, content_hash = normalize(conn, row["kind"], row["form_revision"], json.loads(row["payload_json"]))
        proof = conn.execute(
            "SELECT * FROM org_confirmation WHERE token_hash = ?",
            (hashlib.sha256(confirmation.encode()).hexdigest(),),
        ).fetchone()
        now = utcnow()
        if (proof is None or proof["used_at"] is not None
                or proof["principal_id"] != identity.principal_id or proof["request_id"] != draft_id
                or proof["content_hash"] != content_hash or row["content_hash"] != content_hash
                or row["project_id"] != preview["fields"]["project_id"]
                or datetime.fromisoformat(proof["expires_at"]) <= now):
            _fail(409, "Confirmation is invalid, expired, consumed, or does not match the draft")
        timestamp = now.isoformat()
        conn.execute("UPDATE org_confirmation SET used_at = ? WHERE token_hash = ?", (timestamp, proof["token_hash"]))
        conn.execute(
            "UPDATE service_request SET status = 'submitted', submitted_at = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, draft_id),
        )
        response = _view(_owned(conn, draft_id, identity))
        conn.execute(
            "INSERT INTO org_idempotency (principal_id, operation, key, request_hash,"
            " response_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (identity.principal_id, operation, idempotency_key, request_hash, canonical(response), timestamp),
        )
        return response


def review(identity, request_id, decision, reason):
    if identity.auth_type != "web" or identity.role != "admin":
        _fail(403, "Web administrator required")
    if decision not in ("in_review", "needs_information", "approved", "rejected"):
        _fail(422, "Invalid review decision")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM service_request WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            _fail(404, "Application not found")
        if decision not in TRANSITIONS.get(row["status"], ()):
            _fail(409, "Invalid application state transition")
        now = utcnow().isoformat()
        conn.execute(
            "INSERT INTO service_request_review (id, request_id, reviewer_id, decision, reason, decided_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, request_id, identity.principal_id, decision, reason.strip(), now),
        )
        conn.execute(
            "UPDATE service_request SET status = ?, updated_at = ?, reviewer_id = ?, review_reason = ? WHERE id = ?",
            (decision, now, identity.principal_id, reason.strip(), request_id),
        )
        return {"id": request_id, "status": decision, "reviewer_id": identity.principal_id, "reason": reason.strip(), "reviewed_at": now}


def withdraw(identity, request_id):
    if identity.auth_type != "web":
        _fail(403, "Trusted web request required")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _owned(conn, request_id, identity)
        if "withdrawn" not in TRANSITIONS.get(row["status"], ()):
            _fail(409, "Application cannot be withdrawn in this state")
        conn.execute("UPDATE service_request SET status = 'withdrawn', updated_at = ? WHERE id = ?", (utcnow().isoformat(), request_id))
        conn.execute("DELETE FROM org_confirmation WHERE request_id = ?", (request_id,))
        return _view(_owned(conn, request_id, identity))
