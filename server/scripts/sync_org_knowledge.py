import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db

SOURCE_REPOS = {
    "governance-public": "https://github.com/CUMT-Alethic-Insight-Lab/governance-public",
}
DEFAULT_ALLOWLIST = {
    "source_repo": "governance-public",
    "documents": [
        {"path": "docs/zh-CN/membership.md", "title": "成员制度", "access_level": "public"},
        {"path": "docs/zh-CN/charter.md", "title": "组织章程", "access_level": "public"},
        {"path": "docs/zh-CN/ai-assistance-policy.md", "title": "AI 辅助政策", "access_level": "public"},
    ],
}
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_DOCUMENTS = 100


def sync_documents(local_dir, allowlist=None, revision=None):
    if config.IS_EDGE:
        raise ValueError("knowledge synchronization is restricted to the authoritative instance")
    manifest = DEFAULT_ALLOWLIST if allowlist is None else allowlist
    if not isinstance(manifest, dict) or set(manifest) != {"source_repo", "documents"}:
        raise ValueError("allowlist must contain source_repo and documents")
    repo = manifest["source_repo"]
    if not isinstance(repo, str) or repo not in SOURCE_REPOS:
        raise ValueError("unknown source repository")
    entries = manifest["documents"]
    if not isinstance(entries, list) or len(entries) > MAX_DOCUMENTS:
        raise ValueError("allowlist is limited to 100 documents")
    if revision is not None and not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise ValueError("revision must be a full Git commit hash")
    root = Path(local_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("local source must be a directory")
    prepared = []
    seen = set()
    now = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "title", "access_level"}:
            raise ValueError("each document needs path, title and access_level")
        if entry["access_level"] not in ("public", "member"):
            raise ValueError("only public/member documents may be synchronized")
        name = entry["path"]
        if not isinstance(name, str):
            raise ValueError("invalid document path")
        path = PurePosixPath(name)
        if (not name or "\\" in name or ":" in name or path.is_absolute()
                or path.as_posix() != name or any(part in ("..", ".") or part.startswith(".") for part in path.parts)
                or path.suffix.lower() not in (".md", ".txt") or name in seen):
            raise ValueError("unsafe or duplicate document path")
        title = entry["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("document title must contain 1 to 200 characters")
        seen.add(name)
        target = root.joinpath(*path.parts).resolve(strict=True)
        if (not target.is_relative_to(root) or not target.is_file()
                or any(part.startswith(".") for part in target.relative_to(root).parts)):
            raise ValueError("document must be a non-hidden file inside the local source directory")
        if target.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("document exceeds 256 KiB")
        raw = target.read_bytes()
        if len(raw) > MAX_DOCUMENT_BYTES or b"\x00" in raw:
            raise ValueError("document is oversized or binary")
        content = raw.decode("utf-8-sig")
        content_revision = revision or "sha256:" + hashlib.sha256(raw).hexdigest()
        document_id = hashlib.sha256((repo + ":" + name).encode()).hexdigest()
        source_url = SOURCE_REPOS[repo] + "/blob/" + (revision or "main") + "/" + quote(name, safe="/")
        prepared.append((document_id, title, content, repo, name, source_url,
                         content_revision, entry["access_level"], now, now))
    with db.connection() as conn:
        for row in prepared:
            conn.execute(
                "INSERT INTO org_document"
                " (id, title, content, source_repo, path, source_url, revision, visibility,"
                " updated_at, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(source_repo, path) DO UPDATE SET title = excluded.title, content = excluded.content,"
                " source_url = excluded.source_url, revision = excluded.revision,"
                " visibility = excluded.visibility, updated_at = excluded.updated_at, indexed_at = excluded.indexed_at"
                " WHERE org_document.title != excluded.title OR org_document.content != excluded.content"
                " OR org_document.source_url != excluded.source_url OR org_document.revision != excluded.revision"
                " OR org_document.visibility != excluded.visibility",
                row,
            )
        paths = [row[4] for row in prepared]
        if paths:
            placeholders = ",".join("?" for _ in paths)
            conn.execute(
                f"DELETE FROM org_document WHERE source_repo = ? AND path NOT IN ({placeholders})",
                [repo, *paths],
            )
        else:
            conn.execute("DELETE FROM org_document WHERE source_repo = ?", (repo,))
    return {"source_repo": repo, "imported": len(prepared)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import explicitly approved public/member documents; no network access.")
    parser.add_argument("--local-dir", required=True, help="Local checkout of the approved public source")
    parser.add_argument("--allowlist", help="JSON manifest; defaults to governance-public policy examples")
    parser.add_argument("--revision", help="Full source commit hash; otherwise use per-document SHA-256 revisions")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(Path(args.allowlist).read_text(encoding="utf-8")) if args.allowlist else None
        result = sync_documents(args.local_dir, manifest, args.revision)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Synchronization refused: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
