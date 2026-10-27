import base64
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from . import db
from .org_auth import OrgIdentity

PROJECT_FIELDS = (
    "id", "slug", "title", "description", "lifecycle_status", "recruiting",
    "visibility", "updated_at",
)
DOCUMENT_FIELDS = ("id", "title", "source_url", "revision", "updated_at", "access_level")
MAX_LIMIT = 100
MAX_QUERY = 200
MAX_SNIPPET = 240


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def pagination(limit: int, cursor: str | None, kind: str):
    if not 1 <= limit <= MAX_LIMIT:
        raise HTTPException(422, "limit must be between 1 and 100")
    if not cursor:
        return ""
    try:
        if len(cursor) > 512:
            raise ValueError()
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (not isinstance(data, list) or len(data) != 2 or data[0] != kind
                or not isinstance(data[1], str) or len(data[1]) > 128):
            raise ValueError()
        return data[1]
    except (ValueError, TypeError, IndexError, UnicodeDecodeError):
        raise HTTPException(422, "invalid cursor")


def page(rows, limit, kind, serialize):
    items = [serialize(row) for row in rows[:limit]]
    cursor = None
    if len(rows) > limit:
        cursor = base64.urlsafe_b64encode(
            json.dumps([kind, rows[limit - 1]["id"]]).encode()
        ).decode()
    return {"items": items, "next_cursor": cursor}


def resource_acl(identity: OrgIdentity, alias: str, level: str):
    if identity.role not in ("member", "admin"):
        return "0", []
    # Restricted records never cross a generic CLI boundary, even for an admin.
    if identity.auth_type == "web" and identity.role == "admin":
        return "1", []
    acl = f"{alias}.{level} IN ('public', 'member')"
    if alias == "p":
        acl = f"({acl} OR ({alias}.{level} = 'project' AND {alias}.owner_student_id = ?))"
        return acl, [identity.student_id]
    return acl, []


def project_view(row):
    result = {key: row[key] for key in PROJECT_FIELDS}
    result["recruiting"] = bool(result["recruiting"])
    result["lifecycle"] = result["lifecycle_status"]
    return result


def document_view(row):
    return {key: row["visibility"] if key == "access_level" else row[key] for key in DOCUMENT_FIELDS}


def projects(identity, limit=50, cursor=None, recruiting=None):
    after = pagination(limit, cursor, "projects")
    acl, params = resource_acl(identity, "p", "visibility")
    clause = f"{acl} AND p.id > ?"
    params.append(after)
    if recruiting is not None:
        clause += " AND p.recruiting = ?"
        params.append(int(recruiting))
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT p.* FROM org_project p WHERE {clause} ORDER BY p.id LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
    return page(rows, limit, "projects", project_view)


def project(identity, project_id):
    acl, params = resource_acl(identity, "p", "visibility")
    with db.connection() as conn:
        row = conn.execute(
            f"SELECT p.* FROM org_project p WHERE p.id = ? AND {acl}",
            [project_id, *params],
        ).fetchone()
    if row is None:
        raise HTTPException(404, "project not found")
    return project_view(row)


def save_project(identity, values, project_id=None):
    if identity.auth_type != "web" or identity.role != "admin":
        raise HTTPException(403, "web administrator required")
    now = utcnow()
    try:
        with db.connection() as conn:
            if project_id is None:
                project_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO org_project"
                    " (id, slug, title, description, lifecycle_status, recruiting, visibility,"
                    " owner_student_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'member', ?, ?)",
                    (project_id, values["slug"], values["title"], values.get("description", ""),
                     values.get("lifecycle", "proposed"), int(values.get("recruiting", False)), identity.student_id, now),
                )
            else:
                if conn.execute("SELECT id FROM org_project WHERE id = ?", (project_id,)).fetchone() is None:
                    raise HTTPException(404, "project not found")
                if values:
                    allowed = {"slug", "title", "description", "lifecycle", "recruiting"}
                    if set(values) - allowed:
                        raise HTTPException(422, "unknown project field")
                    columns = ", ".join(f"{'lifecycle_status' if key == 'lifecycle' else key} = ?" for key in values)
                    conn.execute(
                        f"UPDATE org_project SET {columns}, updated_at = ? WHERE id = ?",
                        [*values.values(), now, project_id],
                    )
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (identity.principal_id, "org_project.save", project_id, json.dumps(sorted(values)), now),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "project slug already exists or invalid project")
    return project(identity, project_id)


def document_acl(identity):
    return resource_acl(identity, "d", "visibility")


def search(identity, q, limit=20, cursor=None):
    query = q.strip()
    if not query or len(q) > MAX_QUERY:
        raise HTTPException(422, "q must contain 1 to 200 characters")
    kind = "knowledge:" + uuid.uuid5(uuid.NAMESPACE_URL, query).hex
    after = pagination(limit, cursor, kind)
    acl, params = document_acl(identity)
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = "%" + escaped + "%"
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT d.* FROM org_document d WHERE {acl} AND d.id > ?"
            " AND (d.title LIKE ? ESCAPE '\\' OR d.content LIKE ? ESCAPE '\\')"
            " ORDER BY d.id LIMIT ?",
            [*params, after, pattern, pattern, limit + 1],
        ).fetchall()

    def result(row):
        item = document_view(row)
        content = row["content"]
        index = content.lower().find(query.lower())
        start = max(0, index - 60)
        item["snippet"] = content[start:start + MAX_SNIPPET]
        return item

    return page(rows, limit, kind, result)


def document(identity, document_id):
    acl, params = document_acl(identity)
    with db.connection() as conn:
        row = conn.execute(
            f"SELECT d.* FROM org_document d WHERE d.id = ? AND {acl}",
            [document_id, *params],
        ).fetchone()
    if row is None:
        raise HTTPException(404, "document not found")
    return {**document_view(row), "content": row["content"]}
